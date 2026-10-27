"""Local-only integration smoke test using an original miniature fake Libretro core.
Does NOT validate real mGBA speed; validates native ABI and WebSocket protocol.
"""
import asyncio
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from aiohttp import ClientSession, WSServerHandshakeError, web, WSMsgType

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
os.environ['GBA_TOKEN'] = 'local-smoke-test-token-minimum-24-chars'
os.environ['GBA_ORIGINS'] = 'https://cfx-nui-fsg_gamestation'
sys.path.insert(0, str(ROOT))
from host import NativeGBA, make_app, TOKEN


async def test():
    with tempfile.TemporaryDirectory(prefix='litoral-smoke-') as tmp:
        lib = Path(tmp) / 'mock_core.so'
        rom = Path(tmp) / 'dummy.gba'
        rom.write_bytes(b'GBA_SMOKE_TEST'+b'\0'*1024)
        subprocess.run(['cc', '-shared', '-fPIC', '-O2', '-o', str(lib), str(HERE/'mock_core.c')], check=True)
        core = NativeGBA(lib, rom)
        assert core.fps > 59
        thread = threading.Thread(target=core.run, daemon=True)
        thread.start()
        app = make_app(core)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '127.0.0.1', 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        url = f'http://127.0.0.1:{port}'
        origin = {'Origin': 'https://cfx-nui-fsg_gamestation'}
        async with ClientSession() as session:
            health = await (await session.get(url+'/health')).json()
            assert health['ok'] and health['emulator'] == 'mGBA native / Libretro'
            assert (await session.get(url+'/')).status == 200
            try:
                await session.ws_connect(url+'/play', headers=origin, protocols=['gba-v1', 'gba-key-wrong-secret-xxxxxxxxxxxxxxxxxx'])
                raise AssertionError('Invalid token should be rejected')
            except WSServerHandshakeError as e:
                assert e.status == 401, e.status
            try:
                await session.ws_connect(url+'/play', headers={'Origin': 'https://evil.example'}, protocols=['gba-v1', 'gba-key-'+TOKEN])
                raise AssertionError('Invalid origin should be rejected')
            except WSServerHandshakeError as e:
                assert e.status == 403, e.status
            async with session.ws_connect(url+'/play', headers=origin, protocols=['gba-v1', 'gba-key-'+TOKEN]) as ws:
                jpeg = None
                for _ in range(12):
                    msg = await ws.receive(timeout=3)
                    if msg.type == WSMsgType.BINARY:
                        jpeg = msg.data
                        break
                assert jpeg and jpeg.startswith(b'\xff\xd8') and jpeg.endswith(b'\xff\xd9'), 'No JPEG frame'
                print('First JPEG frame:', len(jpeg),'bytes')
                await ws.send_json({'type':'input', 'mask':1<<8})
                await asyncio.sleep(.12)
                assert core.input_mask == 256
                try:
                    await session.ws_connect(url+'/play', headers=origin, protocols=['gba-v1', 'gba-key-'+TOKEN])
                    raise AssertionError('Second controller should be blocked')
                except WSServerHandshakeError as e:
                    assert e.status == 409, e.status
            await asyncio.sleep(.1)
            assert core.input_mask == 0
            # After leaving, another player can connect.
            async with session.ws_connect(url+'/play', headers=origin, protocols=['gba-v1', 'gba-key-'+TOKEN]):
                pass
            print('Host health/auth/origin/reconnect/controls/frame: OK')
        core.halted.set()
        thread.join(timeout=2)
        await runner.cleanup()

if __name__ == '__main__':
    asyncio.run(test())
