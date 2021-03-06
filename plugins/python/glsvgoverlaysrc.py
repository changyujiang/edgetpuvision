# Copyright 2019 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import cairo
import collections
import contextlib
import ctypes
import os
import threading
import time

import gi
gi.require_version('GObject', '2.0')
gi.require_version('GLib', '2.0')
gi.require_version('Gst', '1.0')
gi.require_version('GstBase', '1.0')
gi.require_version('GstVideo', '1.0')
from gi.repository import GObject, GLib, Gst, GstBase, GstVideo

# Gst.Buffer.map(Gst.MapFlags.WRITE) is broken, this is a workaround. See
# http://lifestyletransfer.com/how-to-make-gstreamer-buffer-writable-in-python/
# https://gitlab.gnome.org/GNOME/gobject-introspection/issues/69
class GstMapInfo(ctypes.Structure):
    _fields_ = [('memory', ctypes.c_void_p),                # GstMemory *memory
                ('flags', ctypes.c_int),                    # GstMapFlags flags
                ('data', ctypes.POINTER(ctypes.c_byte)),    # guint8 *data
                ('size', ctypes.c_size_t),                  # gsize size
                ('maxsize', ctypes.c_size_t),               # gsize maxsize
                ('user_data', ctypes.c_void_p * 4),         # gpointer user_data[4]
                ('_gst_reserved', ctypes.c_void_p * 4)]     # GST_PADDING

# ctypes imports for missing or broken introspection APIs.
libgst = ctypes.CDLL('libgstreamer-1.0.so.0')
GST_MAP_INFO_POINTER = ctypes.POINTER(GstMapInfo)
libgst.gst_buffer_map.argtypes = [ctypes.c_void_p, GST_MAP_INFO_POINTER, ctypes.c_int]
libgst.gst_buffer_map.restype = ctypes.c_int
libgst.gst_buffer_unmap.argtypes = [ctypes.c_void_p, GST_MAP_INFO_POINTER]
libgst.gst_buffer_unmap.restype = None
libgst.gst_mini_object_is_writable.argtypes = [ctypes.c_void_p]
libgst.gst_mini_object_is_writable.restype = ctypes.c_int

libcairo = ctypes.CDLL('libcairo.so.2')
libcairo.cairo_image_surface_create_for_data.restype = ctypes.c_void_p
libcairo.cairo_image_surface_create_for_data.argtypes = [ctypes.c_void_p,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int]
libcairo.cairo_surface_flush.restype = None
libcairo.cairo_surface_flush.argtypes = [ctypes.c_void_p]
libcairo.cairo_surface_destroy.restype = None
libcairo.cairo_surface_destroy.argtypes = [ctypes.c_void_p]
libcairo.cairo_format_stride_for_width.restype = ctypes.c_int
libcairo.cairo_format_stride_for_width.argtypes = [ctypes.c_int, ctypes.c_int]
libcairo.cairo_create.restype = ctypes.c_void_p
libcairo.cairo_create.argtypes = [ctypes.c_void_p]
libcairo.cairo_destroy.restype = None
libcairo.cairo_destroy.argtypes = [ctypes.c_void_p]

librsvg = ctypes.CDLL('librsvg-2.so.2')
librsvg.rsvg_handle_new_from_data.restype = ctypes.c_void_p
librsvg.rsvg_handle_new_from_data.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.c_void_p]
librsvg.rsvg_handle_render_cairo.restype = ctypes.c_bool
librsvg.rsvg_handle_render_cairo.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
librsvg.rsvg_handle_close.restype = ctypes.c_bool
librsvg.rsvg_handle_close.argtypes = [ctypes.c_void_p, ctypes.c_void_p]

libgobject = ctypes.CDLL('libgobject-2.0.so.0')
libgobject.g_object_unref.restype = None
libgobject.g_object_unref.argtypes = [ctypes.c_void_p]

@contextlib.contextmanager
def _gst_buffer_map(buffer, flags):
    ptr = hash(buffer)
    if flags & Gst.MapFlags.WRITE and libgst.gst_mini_object_is_writable(ptr) == 0:
        raise ValueError('Buffer not writable')

    mapping = GstMapInfo()
    success = libgst.gst_buffer_map(ptr, mapping, flags)
    if not success:
        raise RuntimeError('gst_buffer_map failed')
    try:
        yield ctypes.cast(mapping.data, ctypes.POINTER(ctypes.c_byte * mapping.size)).contents
    finally:
        libgst.gst_buffer_unmap(ptr, mapping)

DEFAULT_IS_LIVE = True
DEFAULT_HEIGHT = 640
DEFAULT_WIDTH = 480

class SvgOverlaySource(GstBase.PushSrc):
    __gstmetadata__ = ('SVG overlay source',
                       'Source',
                       'Renders SVG using rsvg & cairo',
                       'Coral <coral-support@google.com>')
    __gsttemplates__ = (Gst.PadTemplate.new('src',
                        Gst.PadDirection.SRC,
                        Gst.PadPresence.ALWAYS,
                        Gst.Caps.from_string(
                        'video/x-raw,format=BGRA,framerate=0/1'
                        )))

    def __init__(self):
        GstBase.PushSrc.__init__(self)
        self.set_format(Gst.Format.TIME)
        self.set_do_timestamp(False)
        self.set_live(DEFAULT_IS_LIVE)
        self.cond = threading.Condition()
        self.width = DEFAULT_WIDTH
        self.height = DEFAULT_HEIGHT
        self.min_stride = 0
        self.flushing = False
        self.eos = False
        self.queue = collections.deque()
        self.print_fps = int(os.environ.get('PRINT_FPS', '0'))
        self.fps_start = 0
        self.input_frames = 0
        self.output_frames = 0

    def reset(self):
        with self.cond:
            self.eos = False
            self.queue.clear()

    def do_decide_allocation(self, query):
        if query.get_n_allocation_pools() > 0:
            pool, size, min_buffers, max_buffers = query.parse_nth_allocation_pool(0)
            query.set_nth_allocation_pool(0, pool, size, min_buffers, min(max_buffers, 3))
        return GstBase.BaseSrc.do_decide_allocation(self, query)

    def do_event(self, event):
        if event.type == Gst.EventType.SEEK:
            _, _, flags, _, _, _, _ = event.parse_seek()
            if flags | Gst.SeekFlags.FLUSH:
                self.send_event(Gst.Event.new_flush_start())
                self.send_event(Gst.Event.new_flush_stop(True))
            self.reset()
            return True
        return GstBase.BaseSrc.do_event(self, event)

    def do_gst_base_src_query(self, query):
        if query.type == Gst.QueryType.LATENCY:
            query.set_latency(self.is_live, 0, Gst.CLOCK_TIME_NONE)
            return True
        else:
            return GstBase.BaseSrc.do_query(self, query)

    def do_start (self):
        self.reset()
        return True

    def do_stop (self):
        self.reset()
        return True

    def set_eos(self):
        with self.cond:
            self.eos = True

    def queue_svg(self, svg:str, pts:GObject.TYPE_UINT64):
        with self.cond:
            self.input_frames += 1
            if self.is_live:
                self.queue.clear()
            self.queue.append((svg, pts))
            self.cond.notify_all()

    def set_flushing(self, flushing):
        with self.cond:
            self.flushing = flushing
            self.cond.notify_all()

    def do_fixate(self, caps):
        s = caps.get_structure(0).copy()
        s.fixate_field_nearest_int("width", self.width)
        s.fixate_field_nearest_int("height", self.height)
        result = Gst.Caps.new_empty()
        result.append_structure(s)
        result = result.fixate()
        return result

    def do_set_caps(self, caps):
        structure = caps.get_structure(0)
        self.width = structure.get_value('width')
        self.height = structure.get_value('height')
        self.min_stride = libcairo.cairo_format_stride_for_width(
                int(cairo.FORMAT_ARGB32), self.width)
        return True

    def do_unlock(self):
        self.set_flushing(True)
        return True

    def do_unlock_stop(self):
        self.set_flushing(False)
        return True

    def get_flow_return_locked(self, default=None):
        if not self.queue and self.eos:
            self.eos = False
            return Gst.FlowReturn.EOS
        if self.flushing:
            return Gst.FlowReturn.FLUSHING
        return default

    def do_gst_push_src_fill(self, buf):
        with self.cond:
            result = self.get_flow_return_locked()
            if result:
                return result

            while not self.queue:
                self.cond.wait()
                result = self.get_flow_return_locked()
                if result:
                    return result

            assert self.queue
            svg, pts = self.queue.popleft()

        self.render_svg(svg, buf)
        buf.pts = pts

        with self.cond:
            self.output_frames += 1
            if not self.fps_start:
                self.fps_start = time.monotonic()

            elapsed = time.monotonic() - self.fps_start
            if self.print_fps and elapsed > self.print_fps:
                print('gloverlaysrc: in {} ({:.2f} fps), out {} ({:.2f} fps)'.format(
                    self.input_frames, self.input_frames / elapsed,
                    self.output_frames, self.output_frames / elapsed))
                self.fps_start = time.monotonic()
                self.input_frames = 0
                self.output_frames = 0
            return self.get_flow_return_locked(Gst.FlowReturn.OK)

    def render_svg(self, svg, buf):
        meta = GstVideo.buffer_get_video_meta(buf)
        if meta:
            assert meta.n_planes == 1
            assert meta.width == self.width
            assert meta.height == self.height
            assert meta.stride[0] >= self.min_stride
            stride = meta.stride[0]
        else:
            stride = self.min_stride

        with _gst_buffer_map(buf, Gst.MapFlags.WRITE) as mapped:
            assert len(mapped) >= stride * self.height

            # Fill with transparency.
            ctypes.memset(ctypes.addressof(mapped), 0, ctypes.sizeof(mapped))

            surface = libcairo.cairo_image_surface_create_for_data(
                    ctypes.addressof(mapped),
                    int(cairo.FORMAT_ARGB32),
                    self.width,
                    self.height,
                    stride)

            # Render the SVG overlay.
            data = svg.encode('utf-8')
            context = libcairo.cairo_create(surface)
            handle = librsvg.rsvg_handle_new_from_data(data, len(data), 0)
            librsvg.rsvg_handle_render_cairo(handle, context)
            librsvg.rsvg_handle_close(handle, 0)
            libgobject.g_object_unref(handle)
            libcairo.cairo_surface_flush(surface)
            libcairo.cairo_surface_destroy(surface)
            libcairo.cairo_destroy(context)

class GlSvgOverlaySource(Gst.Bin):
    __gstmetadata__ = ('GL SVG overlay source',
                       'Source',
                       'Renders SVG to OpenGL textures',
                       'Coral <coral-support@google.com>')
    __gsttemplates__ = (Gst.PadTemplate.new('src',
                        Gst.PadDirection.SRC,
                        Gst.PadPresence.ALWAYS,
                        Gst.Caps.from_string(
                        'video/x-raw(memory:GLMemory),format=RGBA,framerate=0/1'
                        )))
    __gproperties__ = {
        "is-live": (bool,
            "Is live",
            "Whether to act as a live source",
            DEFAULT_IS_LIVE,
            GObject.ParamFlags.READWRITE
            ),
        "width": (int,
            "Frame width",
            "Frame width, also settable via caps negotiation",
            1,
            GLib.MAXINT,
            DEFAULT_WIDTH,
            GObject.ParamFlags.READWRITE
            ),
        "height": (int,
            "Frame height",
            "Frame height, also settable via caps negotiation",
            1,
            GLib.MAXINT,
            DEFAULT_WIDTH,
            GObject.ParamFlags.READWRITE
            ),
        }


    def __init__(self):
        GstBase.PushSrc.__init__(self)
        self.src = SvgOverlaySource()
        self.queue = Gst.ElementFactory.make('queue')
        self.upload = Gst.ElementFactory.make('glupload')
        self.convert = Gst.ElementFactory.make('glcolorconvert')
        self.filter = Gst.ElementFactory.make('capsfilter')

        self.add(self.src)
        self.add(self.queue)
        self.add(self.upload)
        self.add(self.convert)
        self.add(self.filter)

        self.src.link_pads('src', self.queue, 'sink')
        self.queue.link_pads('src', self.upload, 'sink')
        self.upload.link_pads('src', self.convert, 'sink')
        self.convert.link_pads('src', self.filter, 'sink')
        pad = Gst.GhostPad('src', self.filter.get_static_pad('src'))
        self.add_pad(pad)
        self.filter.set_property('caps', Gst.Caps.from_string('video/x-raw(memory:GLMemory),format=RGBA'))

    def do_get_property(self, prop):
        if prop.name == 'is-live':
            return self.src.is_live
        elif prop.name == 'width':
            return self.src.width
        elif prop.name == 'height':
            return self.src.height
        else:
            raise AttributeError('Unknown property %s' % prop.name)

    def do_set_property(self, prop, value):
        if prop.name == 'is-live':
            self.src.set_live(value)
        elif prop.name == 'width':
            self.src.width = value
        elif prop.name == 'height':
            self.src.height = value
        else:
            raise AttributeError('Unknown property %s' % prop.name)

    @GObject.Signal(flags=GObject.SignalFlags.ACTION | GObject.SignalFlags.RUN_LAST)
    def set_eos(self):
        self.src.set_eos()

    @GObject.Signal(flags=GObject.SignalFlags.ACTION | GObject.SignalFlags.RUN_LAST)
    def queue_svg(self, svg:str, pts:GObject.TYPE_UINT64):
        self.src.queue_svg(svg, pts)

__gstelementfactory__ = ("glsvgoverlaysrc", Gst.Rank.NONE, GlSvgOverlaySource)
