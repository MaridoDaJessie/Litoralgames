# Litoral GBA · Codespaces Pilot

Runs Game Boy Advance remotely on **native mGBA/Libretro** and streams JPEG frames over WebSocket. No WebAssembly/JIT is needed on the FiveM player.

**This is a Codespaces testing repository. No ROMs are included or to be committed.**

1. Start Codespaces from this private repo; `.devcontainer` installs `libretro-mgba` from Ubuntu repositories and Python requirements.
2. Upload your own `.gba` into `gba_host/roms/super-mario-advance-4.gba` using the Codespaces explorer. `*.gba` is gitignored.
3. Run `bash gba_host/start.sh`. It generates a local `.env` token. In another terminal, use `grep GBA_TOKEN gba_host/.env`.
4. In PORTS, make port `8765` **Public** temporarily and open its HTTPS URL to test mGBA using the browser. Supply the token.
5. For FiveM edit the GameStation's `web/build/gba-stream/stream-config.js` with `url: 'wss://YOUR-CODESPACE-8765.app.github.dev/play'` and the token.
6. Use GameStation Play to open Super Mario Advance 4. Mario Land 2 stays local and is unchanged.

Only one controller at a time. Video: up to 30 JPEG FPS; GameStation spectator mirroring up to 8 FPS. **No audio or persistent saves in this experiment.** The client-visible token and publicly forwarded Codespaces port are NOT production-grade authentication. Stop Codespaces/close public port after testing. Codespaces can time out and incur charges.

To run protocol tests in Codespaces (no proprietary ROM required):

```bash
.venv/bin/python gba_host/tests/smoke.py
```

See `LEIA_PRIMEIRO_CODESPACES.md` in the download for the complete Portuguese guide.
