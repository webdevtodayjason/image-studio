# Image Studio

Generate 512x512 plates on a **Tiiny AI Pocket Lab**, and keep what you generated.

```bash
python3 studio.py --selfcheck          # prove the install works
python3 studio.py --serve 8430         # open the page
```

Python standard library only. No pip, no node, no build step. The page is one HTML file.

The accent is scope cyan `#4FD0C8`, the colour of a waveform monitor's 100% line, so the page reads as a grading bay rather than as TiinyBench's amber instrument panel.

## What it does

- Reads the Text-to-Image models that are actually installed, from the device. There is no hardcoded list.
- Load and unload from the rail. A model that does not fit the free NPU units is refused here, because the device would accept it and roll it back without saying so. Nothing is evicted unless you ask.
- Live NPU: units used and free out of 100, what is resident and what each costs, and on every model in the picker whether it fits right now. Megabytes are not shown; the device's `memory_total_mb` is not a total.
- Prompt, negative prompt, seed, steps. The seed that ran is saved with the plate so you can reproduce it.
- One inference at a time. A second Generate goes on a visible queue. A load is refused while a generate is running. The gateway cap is 220 seconds.
- Gallery on disk at `~/.local/share/tiiny-image-studio/`, never inside the install directory. An app update does not throw it away.
- Download a PNG. Delete one.

512x512 is the only size the firmware renders. That is not a preference, it is a measurement.

## What it does not

- It does not silently unload a resident model to make room. If the one you picked does not fit, the page says what is resident, what it costs, and you unload it yourself.
- It does not do background removal, object removal, outpainting, or character sheets. Those models are on the box. They are not this version.
- It does not guess request fields per model. When the device refuses, the body of the refusal is what you see.

## Where the files live

| | |
|---|---|
| Gallery | `~/.local/share/tiiny-image-studio/gallery/` |
| Saved address / key | `~/.config/tiiny-image-studio.json` |
| Override gallery root | `TIINY_IMAGE_STUDIO_HOME` |

Each plate is a PNG plus a JSON sidecar with model, prompt, negative prompt, seed, steps, and wall time.

## Device

Same discovery as TiinyBench: `TIINY_BASE` / `TIINY_KEY`, then `~/.tiinyapps/device.json`, then a saved address, then a scan. Paste an address and a key on the page if none of that is set.
