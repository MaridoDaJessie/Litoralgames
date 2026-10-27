#!/usr/bin/env python3
"""Litoral Roleplay GBA — isolated native Libretro/mGBA streaming pilot.

The host runs on GitHub Codespaces, not on FiveM players' PCs. The browser
receives JPEG frames through WebSocket and sends digital joypad button state.
Audio is deliberately disabled in this pilot. Single controller at a time.
"""
from __future__ import annotations

import asyncio
import ctypes as C
import io
import json
import os
from pathlib import Path
import secrets
import sys
import threading
import time

from aiohttp import web, WSMsgType
import numpy as np
from PIL import Image

BASE = Path(__file__).resolve().parent
ROM = Path(os.getenv('GBA_ROM', str(BASE / 'roms' / 'super-mario-advance-4.gba'))).expanduser().resolve()
BIND = os.getenv('GBA_BIND', '0.0.0.0')
PORT = int(os.getenv('GBA_PORT', '8765'))
TOKEN = os.getenv('GBA_TOKEN', '')
ORIGINS = set(s.strip() for s in os.getenv('GBA_ORIGINS',
                 'https://cfx-nui-fsg_gamestation,https://nui-game-internal').split(',') if s.strip())
STREAM_FPS = min(max(int(os.getenv('GBA_STREAM_FPS', '30')), 5), 30)
JPEG_QUALITY = min(max(int(os.getenv('GBA_JPEG_QUALITY', '60')), 30), 80)


def find_core() -> Path | None:
    explicit = os.getenv('GBA_CORE', '').strip()
    if explicit:
        p = Path(explicit).expanduser().resolve()
        return p if p.is_file() else None
    for candidate in (
        '/usr/lib/x86_64-linux-gnu/libretro/mgba_libretro.so',
        '/usr/lib/aarch64-linux-gnu/libretro/mgba_libretro.so',
        '/usr/lib/libretro/mgba_libretro.so',
        str(BASE / ('mgba_libretro.dll' if sys.platform == 'win32' else 'mgba_libretro.so')),
    ):
        p = Path(candidate)
        if p.is_file():
            return p
    for parent in (Path('/usr/lib'), Path('/usr/local/lib')):
        if parent.exists():
            match = next(parent.glob('**/mgba_libretro.so'), None)
            if match is not None:
                return match
    return None


# Libretro ABI: https://github.com/libretro/libretro-common/blob/master/include/libretro.h
ENV_GET_CAN_DUPE = 3
ENV_GET_SYSTEM_DIRECTORY = 9
ENV_SET_PIXEL_FORMAT = 10
ENV_GET_VARIABLE = 15
ENV_GET_VARIABLE_UPDATE = 17
ENV_GET_SAVE_DIRECTORY = 31
ENV_GET_LANGUAGE = 39
ENV_GET_CORE_OPTIONS_VERSION = 52
ENV_CB = C.CFUNCTYPE(C.c_bool, C.c_uint, C.c_void_p)
VIDEO_CB = C.CFUNCTYPE(None, C.c_void_p, C.c_uint, C.c_uint, C.c_size_t)
AUDIO_CB = C.CFUNCTYPE(None, C.c_int16, C.c_int16)
AUDIO_BATCH_CB = C.CFUNCTYPE(C.c_size_t, C.c_void_p, C.c_size_t)
INPUT_POLL_CB = C.CFUNCTYPE(None)
INPUT_STATE_CB = C.CFUNCTYPE(C.c_int16, C.c_uint, C.c_uint, C.c_uint, C.c_uint)


class GameInfo(C.Structure):
    _fields_ = [('path', C.c_char_p), ('data', C.c_void_p), ('size', C.c_size_t), ('meta', C.c_char_p)]


class Geometry(C.Structure):
    _fields_ = [('base_width', C.c_uint), ('base_height', C.c_uint),
                ('max_width', C.c_uint), ('max_height', C.c_uint), ('aspect_ratio', C.c_float)]


class Timing(C.Structure):
    _fields_ = [('fps', C.c_double), ('sample_rate', C.c_double)]


class AvInfo(C.Structure):
    _fields_ = [('geometry', Geometry), ('timing', Timing)]


class NativeGBA:
    def __init__(self, core_file: Path, rom_file: Path):
        self.lib = C.CDLL(str(core_file))
        self.rom_file = rom_file
        rom = rom_file.read_bytes()
        if not (192 <= len(rom) <= 32 * 1024 * 1024):
            raise ValueError('ROM inválida: tamanho inesperado')
        if rom_file.suffix.lower() != '.gba':
            raise ValueError('O piloto foi configurado somente para ROM .gba')
        self.rom_data = C.create_string_buffer(rom)
        self.pixel_format = 0   # 0RGB1555, 1 XRGB8888, 2 RGB565
        self.frame_lock = threading.Lock()
        self.frame = None
        self.frame_index = 0
        self.input_mask = 0
        self.last_input_time = 0.0
        self.run_rate = 0.0
        self.fps = 59.7275
        self.reset_requested = False
        self.halted = threading.Event()
        self.sys_dir = C.create_string_buffer(os.fsencode(str(BASE)))
        self._attach_callbacks()
        self._initialize()

    def _bind(self, name, restype, *types):
        f = getattr(self.lib, name)
        f.restype = restype
        f.argtypes = list(types)
        return f

    def _attach_callbacks(self):
        @ENV_CB
        def env(command, data):
            if command == ENV_SET_PIXEL_FORMAT and data:
                pf = C.cast(data, C.POINTER(C.c_int))[0]
                if pf not in (0, 1, 2):
                    return False
                self.pixel_format = pf
                return True
            if command in (ENV_GET_SYSTEM_DIRECTORY, ENV_GET_SAVE_DIRECTORY) and data:
                C.cast(data, C.POINTER(C.c_char_p))[0] = C.cast(self.sys_dir, C.c_char_p)
                return True
            if command in (ENV_GET_CAN_DUPE, ENV_GET_VARIABLE_UPDATE) and data:
                C.cast(data, C.POINTER(C.c_bool))[0] = False
                return True
            if command in (ENV_GET_CORE_OPTIONS_VERSION, ENV_GET_LANGUAGE) and data:
                C.cast(data, C.POINTER(C.c_uint))[0] = 0
                return True
            if command == ENV_GET_VARIABLE:
                return False
            return False

        @VIDEO_CB
        def video(ptr, width, height, pitch):
            if not ptr or width < 1 or height < 1 or width > 1024 or height > 1024:
                return
            bpp = 4 if self.pixel_format == 1 else 2
            if pitch < width * bpp or pitch > 8192:
                return
            try:
                raw = C.string_at(ptr, pitch * height)
                with self.frame_lock:
                    self.frame = (raw, int(width), int(height), int(pitch), self.pixel_format)
                    self.frame_index += 1
            except Exception:
                pass

        @AUDIO_CB
        def audio(_left, _right):
            pass

        @AUDIO_BATCH_CB
        def audio_batch(_data, frames):
            return frames

        @INPUT_POLL_CB
        def input_poll():
            pass

        @INPUT_STATE_CB
        def input_state(port, device, index, button):
            # RETRO_DEVICE_JOYPAD == 1; all controls are digital IDs 0..15.
            if port != 0 or device != 1 or index != 0 or button > 15:
                return 0
            if time.monotonic() - self.last_input_time > 1.0:
                return 0
            return 1 if self.input_mask & (1 << button) else 0

        self.callbacks = (env, video, audio, audio_batch, input_poll, input_state)
        names = ('retro_set_environment', 'retro_set_video_refresh',
                 'retro_set_audio_sample', 'retro_set_audio_sample_batch',
                 'retro_set_input_poll', 'retro_set_input_state')
        for name, cb in zip(names, self.callbacks):
            self._bind(name, None, type(cb))(cb)

    def _initialize(self):
        self._bind('retro_init', None)()
        version = self._bind('retro_api_version', C.c_uint)()
        if version != 1:
            raise RuntimeError('ABI Libretro incompatível: ' + str(version))
        load = self._bind('retro_load_game', C.c_bool, C.POINTER(GameInfo))
        info = GameInfo(os.fsencode(str(self.rom_file)),
                        C.cast(self.rom_data, C.c_void_p), len(self.rom_data) - 1, None)
        if not load(C.byref(info)):
            raise RuntimeError('mGBA não conseguiu iniciar a ROM')
        av = AvInfo()
        self._bind('retro_get_system_av_info', None, C.POINTER(AvInfo))(C.byref(av))
        if 30.0 <= av.timing.fps <= 120.0:
            self.fps = av.timing.fps
        self._bind('retro_run', None)
        self._bind('retro_reset', None)
        # Explicitly select joypad, matching the GBA buttons sent by GameStation.
        if hasattr(self.lib, 'retro_set_controller_port_device'):
            self._bind('retro_set_controller_port_device', None, C.c_uint, C.c_uint)(0, 1)
        self._bind('retro_unload_game', None)
        self._bind('retro_deinit', None)

    def run(self):
        frame_seconds = 1.0 / self.fps
        next_tick = time.perf_counter()
        window_start = time.perf_counter()
        cycles = 0
        try:
            while not self.halted.is_set():
                if self.reset_requested:
                    self.lib.retro_reset()
                    self.reset_requested = False
                self.lib.retro_run()
                cycles += 1
                now = time.perf_counter()
                if now - window_start >= 2.0:
                    self.run_rate = cycles / (now - window_start)
                    window_start, cycles = now, 0
                next_tick += frame_seconds
                if next_tick < now - 0.25:
                    next_tick = now
                remaining = next_tick - now
                if remaining > 0:
                    time.sleep(remaining)
        finally:
            self.lib.retro_unload_game()
            self.lib.retro_deinit()

    def set_mask(self, mask: int):
        self.input_mask = mask & 0xFFFF
        self.last_input_time = time.monotonic()

    def current_jpeg(self):
        with self.frame_lock:
            frame = self.frame
            version = self.frame_index
        if frame is None:
            return version, None
        data, width, height, pitch, fmt = frame
        if fmt == 1:
            image = Image.frombytes('RGB', (width, height), data, 'raw', 'BGRX', pitch, 1)
        else:
            samples = np.ndarray((height, pitch // 2), dtype='<u2', buffer=data)[:, :width]
            if fmt == 2:
                r = ((samples >> 11) & 31).astype(np.uint8) << 3
                g = ((samples >> 5) & 63).astype(np.uint8) << 2
                b = (samples & 31).astype(np.uint8) << 3
            else:
                r = ((samples >> 10) & 31).astype(np.uint8) << 3
                g = ((samples >> 5) & 31).astype(np.uint8) << 3
                b = (samples & 31).astype(np.uint8) << 3
            image = Image.fromarray(np.stack((r, g, b), axis=2), mode='RGB')
        if image.size != (240, 160):
            image = image.resize((240, 160))
        buf = io.BytesIO()
        image.save(buf, format='JPEG', quality=JPEG_QUALITY, optimize=False)
        return version, buf.getvalue()


def parse_bearer_protocol(request) -> str:
    items = (request.headers.get('Sec-WebSocket-Protocol') or '').split(',')
    for p in items:
        p = p.strip()
        if p.startswith('gba-key-'):
            return p[len('gba-key-'):]
    return ''


def allowed_origin(request):
    origin = request.headers.get('Origin', '')
    if origin in ORIGINS:
        return True
    # Allow same-origin browser preview of the Codespaces forwarded port.
    host = request.headers.get('Host', '')
    if host.endswith('.app.github.dev') and origin == ('https://' + host):
        return True
    return (host.startswith('127.0.0.1:') or host.startswith('localhost:')) and origin == ('http://' + host)


class Host:
    def __init__(self, core):
        self.core = core
        self.controller = None
        self.controller_lock = asyncio.Lock()

    async def health(self, _request):
        return web.json_response({
            'ok': True, 'emulator': 'mGBA native / Libretro',
            'cyclesPerSecond': round(self.core.run_rate, 1),
            'expectedCyclesPerSecond': round(self.core.fps, 2),
            'maxVideoFps': STREAM_FPS, 'busy': self.controller is not None,
            'audio': False,
            'note': 'cyclesPerSecond é velocidade da emulação nativa, não FPS do FiveM',
        }, headers={'Cache-Control': 'no-store'})

    async def demo(self, _request):
        return web.FileResponse(BASE / 'demo' / 'index.html')

    async def websocket(self, request):
        if not allowed_origin(request):
            raise web.HTTPForbidden(text='Origem do navegador não autorizada')
        received = parse_bearer_protocol(request)
        if len(TOKEN) < 24 or not secrets.compare_digest(received, TOKEN):
            raise web.HTTPUnauthorized(text='Credencial de teste inválida')
        async with self.controller_lock:
            if self.controller is not None:
                raise web.HTTPConflict(text='Uma partida já está em andamento')
            ws = web.WebSocketResponse(protocols=('gba-v1',), heartbeat=10,
                                       compress=False, max_msg_size=4096)
            await ws.prepare(request)
            self.controller = ws
            self.core.set_mask(0)
            self.core.reset_requested = True

        finished = asyncio.Event()

        async def send_frames():
            last_version = -1
            last_jpeg_sent = 0.0
            last_stats = time.monotonic()
            sent_window = 0
            while not ws.closed:
                version, jpeg = self.core.current_jpeg()
                now = time.monotonic()
                if jpeg and version != last_version and now-last_jpeg_sent >= (1.0/STREAM_FPS)*.92:
                    await ws.send_bytes(jpeg)
                    last_version = version
                    last_jpeg_sent = now
                    sent_window += 1
                if now - last_stats >= 2.0:
                    await ws.send_json({'type': 'stats', 'runRate': round(self.core.run_rate, 1),
                                        'streamFps': round(sent_window / (now-last_stats), 1)})
                    sent_window, last_stats = 0, now
                try:
                    await asyncio.wait_for(finished.wait(), timeout=1.0 / STREAM_FPS)
                except asyncio.TimeoutError:
                    pass

        sender = asyncio.create_task(send_frames())
        try:
            async for message in ws:
                if message.type == WSMsgType.TEXT:
                    try:
                        action = json.loads(message.data)
                        if isinstance(action, dict):
                            if action.get('type') == 'input' and type(action.get('mask')) is int and 0 <= action['mask'] <= 65535:
                                self.core.set_mask(action['mask'])
                            elif action.get('type') == 'reset':
                                self.core.reset_requested = True
                    except (json.JSONDecodeError, ValueError, TypeError):
                        pass
                elif message.type == WSMsgType.ERROR:
                    break
        finally:
            finished.set()
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)
            self.core.set_mask(0)
            self.core.last_input_time = 0
            async with self.controller_lock:
                if self.controller is ws:
                    self.controller = None
        return ws


def make_app(core):
    h = Host(core)
    app = web.Application(client_max_size=4096)
    app.router.add_get('/', h.demo)
    app.router.add_get('/health', h.health)
    app.router.add_get('/play', h.websocket)
    return app


def main():
    if len(TOKEN) < 24:
        sys.exit('Configure GBA_TOKEN (mínimo 24 caracteres) no .env local')
    core_path = find_core()
    if core_path is None:
        sys.exit('Núcleo mGBA Libretro ausente. No Codespaces: sudo apt-get install libretro-mgba')
    if not ROM.is_file():
        sys.exit('ROM não encontrada em ' + str(ROM) +
                 '\nArraste seu arquivo .gba para gba_host/roms/ dentro do Codespaces; ele é ignorado pelo Git.')
    core = NativeGBA(core_path, ROM)
    thread = threading.Thread(target=core.run, daemon=True, name='libretro-gba')
    thread.start()
    print(f'LITORAL GBA STREAM: core={core_path} rom={ROM.name} FPS alvo={core.fps:.2f}')
    print(f'Acesso Codespaces: http://localhost:{PORT} (GitHub encaminhará a porta)')
    try:
        web.run_app(make_app(core), host=BIND, port=PORT, access_log=None, print=None)
    finally:
        core.halted.set()
        thread.join(timeout=4)


if __name__ == '__main__':
    main()
