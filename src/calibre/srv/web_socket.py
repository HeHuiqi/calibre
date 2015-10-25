#!/usr/bin/env python2
# vim:fileencoding=utf-8
# License: GPLv3 Copyright: 2015, Kovid Goyal <kovid at kovidgoyal.net>

from __future__ import (unicode_literals, division, absolute_import,
                        print_function)

import codecs, httplib, struct, os, weakref, repr as reprlib, time, socket
from base64 import standard_b64encode
from functools import partial
from hashlib import sha1
from io import BytesIO
from Queue import Queue, Empty
from threading import Lock, Thread

from calibre import as_unicode
from calibre.srv.loop import ServerLoop, HandleInterrupt, WRITE, READ, RDWR, Connection
from calibre.srv.http_response import HTTPConnection, create_http_handler
from calibre.srv.utils import DESIRED_SEND_BUFFER_SIZE

HANDSHAKE_STR = (
    "HTTP/1.1 101 Switching Protocols\r\n"
    "Upgrade: WebSocket\r\n"
    "Connection: Upgrade\r\n"
    "Sec-WebSocket-Accept: %s\r\n\r\n"
)
GUID_STR = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'

CONTINUATION = 0x0
TEXT = 0x1
BINARY = 0x2
CLOSE = 0x8
PING = 0x9
PONG = 0xA
CONTROL_CODES = (CLOSE, PING, PONG)
ALL_CODES = CONTROL_CODES + (CONTINUATION, TEXT, BINARY)

CHUNK_SIZE = 16 * 1024
SEND_CHUNK_SIZE = DESIRED_SEND_BUFFER_SIZE - 16

NORMAL_CLOSE = 1000
SHUTTING_DOWN = 1001
PROTOCOL_ERROR = 1002
UNSUPPORTED_DATA = 1003
INCONSISTENT_DATA = 1007
POLICY_VIOLATION = 1008
MESSAGE_TOO_BIG = 1009
UNEXPECTED_ERROR = 1011

class ReadFrame(object):  # {{{

    def __init__(self):
        self.state = self.read_header0

    def read_header0(self, conn):
        data = conn.recv(1)
        if not data:
            return
        b = ord(data)
        self.fin = b & 0b10000000
        self.opcode = b & 0b1111
        self.state = self.read_header1
        if self.opcode not in ALL_CODES:
            conn.log.error('Unknown OPCODE from client: %r' % self.opcode)
            conn.websocket_close(PROTOCOL_ERROR, 'Unknown OPCODE: %r' % self.code)
            return

    def read_header1(self, conn):
        data = conn.recv(1)
        if not data:
            return
        b = ord(data)
        self.mask = b & 0b10000000
        if not self.mask:
            conn.log.error('Unmasked packet from client')
            conn.websocket_close(PROTOCOL_ERROR, 'Unmasked packet not allowed')
            return
        self.payload_length = b & 0b01111111
        if self.opcode in (PING, PONG) and self.payload_length > 125:
            conn.log.error('Too large ping packet from client')
            conn.websocket_close(PROTOCOL_ERROR, 'Ping packet too large')
            return
        self.mask_buf = b''
        if self.payload_length == 126:
            self.plbuf = b''
            self.state = partial(self.read_payload_length, 2)
        elif self.payload_length == 127:
            self.plbuf = b''
            self.state = partial(self.read_payload_length, 8)
        else:
            self.state = self.read_masking_key

    def read_payload_length(self, size_in_bytes, conn):
        num_left = size_in_bytes - len(self.plbuf)
        data = conn.recv(num_left)
        if not data:
            return
        self.plbuf += data
        if len(self.plbuf) < size_in_bytes:
            return
        fmt = b'!H' if size_in_bytes == 2 else b'!Q'
        self.payload_length = struct.unpack(fmt, self.plbuf)[0]
        del self.plbuf
        self.state = self.read_masking_key

    def read_masking_key(self, conn):
        num_left = 4 - len(self.mask_buf)
        data = conn.recv(num_left)
        if not data:
            return
        self.mask_buf += data
        if len(self.mask_buf) < 4:
            return
        self.mask = bytearray(self.mask_buf)
        del self.mask_buf
        self.state = self.read_payload
        self.pos = 0
        self.frame_starting = True
        if self.payload_length == 0:
            conn.ws_data_received(b'', self.opcode, True, True, bool(self.fin))
            self.state = None

    def read_payload(self, conn):
        bytes_left = self.payload_length - self.pos
        if bytes_left > 0:
            data = conn.recv(min(bytes_left, CHUNK_SIZE))
            if not data:
                return
        else:
            data = b''
        data = bytearray(data)
        for i in xrange(len(data)):
            data[i] ^= self.mask[(self.pos + i) & 3]
        data = bytes(data)
        self.pos += len(data)
        conn.ws_data_received(data, self.opcode, self.frame_starting, self.pos >= self.payload_length, bool(self.fin))
        self.frame_starting = False
# }}}

# Sending frames {{{

def create_frame(fin, opcode, payload, mask=None, rsv=0):
    if isinstance(payload, type('')):
        payload = payload.encode('utf-8')
    l = len(payload)
    opcode &= 0b1111
    b1 = opcode | (0b10000000 if fin else 0)
    b2 = rsv | (0 if mask is None else 0b10000000)
    if l < 126:
        header = bytes(bytearray((b1, b2 | l)))
    elif 126 <= l <= 65535:
        header = bytes(bytearray((b1, b2 | 126))) + struct.pack(b'!H', l)
    else:
        header = bytes(bytearray((b1, b2 | 127))) + struct.pack(b'!Q', l)
    if mask is not None:
        header += mask
        mask = bytearray(mask)
        payload = bytearray(payload)
        for i in xrange(len(payload)):
            payload[i] ^= mask[i & 3]
        payload = bytes(payload)

    return header + payload


class MessageWriter(object):

    def __init__(self, buf, mask=None, chunk_size=None):
        self.buf, self.data_type, self.mask = buf, BINARY, mask
        if isinstance(buf, type('')):
            self.buf, self.data_type = BytesIO(buf.encode('utf-8')), TEXT
        elif isinstance(buf, bytes):
            self.buf = BytesIO(buf)
        buf = self.buf
        self.chunk_size = chunk_size or SEND_CHUNK_SIZE
        try:
            pos = buf.tell()
            buf.seek(0, os.SEEK_END)
            self.size = buf.tell() - pos
            buf.seek(pos)
        except Exception:
            self.size = None
        self.first_frame_created = self.exhausted = False

    def create_frame(self):
        if self.exhausted:
            return None
        buf = self.buf
        raw = buf.read(self.chunk_size)
        has_more = True if self.size is None else self.size > buf.tell()
        fin = 0 if has_more and raw else 1
        opcode = 0 if self.first_frame_created else self.data_type
        self.first_frame_created, self.exhausted = True, bool(fin)
        return BytesIO(create_frame(fin, opcode, raw, self.mask))
# }}}

conn_id = 0

class WebSocketConnection(HTTPConnection):

    in_websocket_mode = False
    websocket_handler = None

    def __init__(self, *args, **kwargs):
        global conn_id
        HTTPConnection.__init__(self, *args, **kwargs)
        self.sendq = Queue()
        self.control_frames = []
        self.cf_lock = Lock()
        self.sending = None
        self.send_buf = None
        self.frag_decoder = codecs.getincrementaldecoder('utf-8')(errors='strict')
        self.ws_close_received = self.ws_close_sent = False
        conn_id += 1
        self.websocket_connection_id = conn_id

    def finalize_headers(self, inheaders):
        upgrade = inheaders.get('Upgrade', None)
        key = inheaders.get('Sec-WebSocket-Key', None)
        conn = inheaders.get('Connection', None)
        if key is None or upgrade.lower() != 'websocket' or conn != 'Upgrade':
            return HTTPConnection.finalize_headers(self, inheaders)
        ver = inheaders.get('Sec-WebSocket-Version', 'Unknown')
        try:
            ver_ok = int(ver) >= 13
        except Exception:
            ver_ok = False
        if not ver_ok:
            return self.simple_response(httplib.BAD_REQUEST, 'Unsupported WebSocket protocol version: %s' % ver)
        if self.method != 'GET':
            return self.simple_response(httplib.BAD_REQUEST, 'Invalid WebSocket method: %s' % self.method)

        response = HANDSHAKE_STR % standard_b64encode(sha1(key + GUID_STR).digest())
        self.optimize_for_sending_packet()
        self.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.set_state(WRITE, self.upgrade_connection_to_ws, BytesIO(response.encode('ascii')), inheaders)

    def upgrade_connection_to_ws(self, buf, inheaders, event):
        if self.write(buf):
            if self.websocket_handler is None:
                self.websocket_handler = DummyHandler()
            self.current_recv_frame, self.current_recv_opcode = ReadFrame(), None
            self.in_websocket_mode = True
            try:
                self.websocket_handler.handle_websocket_upgrade(self.websocket_connection_id, weakref.ref(self), inheaders)
            except Exception as err:
                self.log.exception('Error in WebSockets upgrade handler:')
                self.websocket_close(UNEXPECTED_ERROR, 'Unexpected error in handler: %r' % as_unicode(err))
            self.set_ws_state()
            self.end_send_optimization()

    def set_ws_state(self):
        if self.ws_close_sent or self.ws_close_received:
            if self.ws_close_sent:
                self.set_state(READ, self.ws_duplex)
                if self.ws_close_received:
                    self.ready = False
            else:
                self.set_state(WRITE, self.ws_duplex)
            return

        if self.send_buf is not None or self.sending is not None:
            self.set_state(RDWR, self.ws_duplex)
        else:
            try:
                self.sending = self.sendq.get_nowait()
            except Empty:
                with self.cf_lock:
                    if self.control_frames:
                        self.set_state(RDWR, self.ws_duplex)
                    else:
                        self.set_state(READ, self.ws_duplex)
            else:
                self.set_state(RDWR, self.ws_duplex)

    def ws_duplex(self, event):
        if event is READ:
            self.ws_read()
        elif event is WRITE:
            self.ws_write()
        self.set_ws_state()

    def ws_read(self):
        self.current_recv_frame.state(self)

    def ws_data_received(self, data, opcode, frame_starting, frame_finished, is_final_frame_of_message):
        if opcode in CONTROL_CODES:
            return self.ws_control_frame(opcode, data)

        message_starting = self.current_recv_opcode is None
        if message_starting:
            self.current_recv_opcode = opcode
        else:
            if opcode != CONTINUATION:
                # This is a new message
                try:
                    self.handle_websocket_data('' if self.current_recv_opcode == TEXT else b'', False, True)
                except Exception as err:
                    self.log.exception('Error in WebSockets data handler:')
                    self.websocket_close(UNEXPECTED_ERROR, 'Unexpected error in handler: %r' % as_unicode(err))
                self.current_recv_opcode = opcode
                message_starting = True
        if frame_finished:
            self.current_recv_frame = ReadFrame()
        message_finished = frame_finished and is_final_frame_of_message
        if message_finished:
            self.current_recv_opcode = None
        if opcode == TEXT:
            if message_starting:
                self.frag_decoder.reset()
            try:
                data = self.frag_decoder.decode(data, final=message_finished)
            except ValueError:
                self.frag_decoder.reset()
                return self.websocket_close(INCONSISTENT_DATA, 'Not valid UTF-8')
        if message_finished:
            self.frag_decoder.reset()
        try:
            self.handle_websocket_data(data, message_starting, message_finished)
        except Exception as err:
            self.log.exception('Error in WebSockets data handler:')
            self.websocket_close(UNEXPECTED_ERROR, 'Unexpected error in handler: %r' % as_unicode(err))

    def ws_control_frame(self, opcode, data):
        if opcode in (PING, CLOSE):
            rcode = PONG if opcode == PING else CLOSE
            f = BytesIO(create_frame(1, rcode, data))
            f.is_close_frame = opcode == CLOSE
            with self.cf_lock:
                self.control_frames.append(f)
            if opcode == CLOSE:
                self.ws_close_received = True
        self.set_ws_state()

    def websocket_close(self, code=NORMAL_CLOSE, reason=b''):
        if isinstance(reason, type('')):
            reason = reason.encode('utf-8')
        reason = reason[:123]
        if code is None and not reason:
            f = BytesIO(create_frame(1, CLOSE, b''))
        else:
            f = BytesIO(create_frame(1, CLOSE, struct.pack(b'!H', code) + reason))
        f.is_close_frame = True
        with self.cf_lock:
            self.control_frames.append(f)
        self.set_ws_state()

    def ws_write(self):
        if self.ws_close_sent:
            return
        if self.send_buf is not None:
            if self.write(self.send_buf):
                self.end_send_optimization()
                if getattr(self.send_buf, 'is_close_frame', False):
                    self.ws_close_sent = True
                self.send_buf = None
        else:
            with self.cf_lock:
                try:
                    self.send_buf = self.control_frames.pop()
                except IndexError:
                    if self.sending is not None:
                        self.send_buf = self.sending.create_frame()
                        if self.send_buf is None:
                            self.sending = None
            if self.send_buf is not None:
                self.optimize_for_sending_packet()

    def close(self):
        if self.in_websocket_mode:
            try:
                self.websocket_handler.handle_websocket_close(self.websocket_connection_id)
            except Exception:
                self.log.exception('Error in WebSocket close handler')
            # Try to write a close frame, just once
            try:
                if self.send_buf is None:
                    self.websocket_close(SHUTTING_DOWN, 'Shutting down')
                    self.write(self.control_frames.pop())
            except Exception:
                pass
            Connection.close(self)
        else:
            HTTPConnection.close(self)

    def send_websocket_message(self, buf, wakeup=True):
        self.sendq.put(MessageWriter(buf))
        self.wait_for = RDWR
        if wakeup:
            self.wakeup()

    def handle_websocket_data(self, data, message_starting, message_finished):
        self.websocket_handler.handle_websocket_data(data, message_starting, message_finished, self.websocket_connection_id)

class DummyHandler(object):

    def handle_websocket_upgrade(self, connection_id, connection_ref, inheaders):
        conn = connection_ref()
        conn.websocket_close(NORMAL_CLOSE, 'No WebSocket handler available')

    def handle_websocket_data(self, data, message_starting, message_finished, connection_id):
        pass

    def handle_websocket_close(self, connection_id):
        pass

# Testing {{{
class EchoClientHandler(object):

    def __init__(self, *args, **kwargs):
        self.msg_buf = []
        self.ws_connections = {}

    def conn(self, cid):
        ans = self.ws_connections.get(cid)
        if ans is not None:
            ans = ans()
        return ans

    def handle_websocket_upgrade(self, connection_id, connection_ref, inheaders):
        self.ws_connections[connection_id] = connection_ref

    def handle_websocket_data(self, data, message_starting, message_finished, connection_id):
        if message_starting:
            self.msg_buf = []
        self.msg_buf.append(data)
        if message_finished:
            j = '' if isinstance(self.msg_buf[0], type('')) else b''
            msg = j.join(self.msg_buf)
            self.msg_buf = []
            print('Received message from client:', reprlib.repr(msg))
            self.conn(connection_id).send_websocket_message(msg)

    def handle_websocket_close(self, connection_id):
        self.ws_connections.pop(connection_id, None)

class EchoServerHandler(object):

    def __init__(self, *args, **kwargs):
        self.msg_buf = []
        self.ws_connections = {}
        t = Thread(name='StdinReader', target=self.get_input)
        t.start()

    def conn(self, cid):
        ans = self.ws_connections.get(cid)
        if ans is not None:
            ans = ans()
        return ans

    def handle_websocket_upgrade(self, connection_id, connection_ref, inheaders):
        self.ws_connections[connection_id] = connection_ref

    def handle_websocket_data(self, data, message_starting, message_finished, connection_id):
        if message_starting:
            self.msg_buf = []
        self.msg_buf.append(data)
        if message_finished:
            j = '' if isinstance(self.msg_buf[0], type('')) else b''
            msg = j.join(self.msg_buf)
            self.msg_buf = []
            print('Received message from client:', msg)

    def get_input(self):
        time.sleep(0.5)
        try:
            while True:
                try:
                    raw = raw_input('Enter some text: ')
                    if not raw:
                        break
                    raw = raw.decode('utf-8')
                    for conn in self.ws_connections.itervalues():
                        conn = conn()
                        if conn is not None:
                            conn.send_websocket_message(raw)
                except (EOFError, KeyboardInterrupt):
                    break
        finally:
            for conn in self.ws_connections.itervalues():
                conn = conn()
                if conn is not None:
                    conn.close()
            print('\nUse Ctrl+C to exit server loop')

    def handle_websocket_close(self, connection_id):
        self.ws_connections.pop(connection_id, None)

if __name__ == '__main__':
    s = ServerLoop(create_http_handler(websocket_handler=EchoClientHandler()))
    with HandleInterrupt(s.wakeup):
        s.serve_forever()
# }}}