# Image Studio

Generate 512x512 plates on a **Tiiny AI Pocket Lab**, read an upload with a vision model, and chain those steps.

```bash
python3 studio.py --selfcheck          # prove the install works
python3 studio.py --serve 8430         # open the page
```

Python standard library only. No pip, no node, no build step. The page is one HTML file.

The accent is scope cyan `#4FD0C8`, the colour of a waveform monitor's 100% line, so the page reads as a grading bay rather than as TiinyBench's amber instrument panel.

## What it does

- Reads the models that are actually installed, from the device, and routes on the task field. There is no hardcoded list.
- Three step kinds, matching the hardware: **read** (image to a Image-Text-to-Text model), **think** (text to a chat model), **render** (text to a Text-to-Image model).
- Presets: Describe, Re-render with a change, Refine, Restyle. Or build a custom chain from the three kinds.
- Drag or pick a png. Longest edge is downscaled to 1536, aspect kept. jpeg and webp are refused with a sentence; this app has to run on Linux in the farm, so there is no Mac converter. Uploads live in the gallery and become the parent of anything derived from them.
- Load and unload from the rail. A model that does not fit the free NPU units is refused here, because the device would accept it and roll it back without saying so. Nothing is evicted unless you ask, except a workflow that already warned it must swap between steps.
- Live NPU: units used and free out of 100, what is resident and what each costs, and on every model in the picker whether it fits right now. Megabytes are not shown; the device's `memory_total_mb` is not a total.
- One inference at a time. A second job goes on a visible queue. The page shows which step of how many is running. A load is refused while a generate is running. The gateway cap is 220 seconds per request.
- The seed that ran is saved with the plate so you can reproduce it.
- Gallery on disk at `~/.local/share/tiiny-image-studio/`, never inside the install directory. An app update does not throw it away.

512x512 is the only size the firmware renders. That is not a preference, it is a measurement.

## Honesty

A chain that starts from an upload and ends in a render does not edit the image. It looks at it and paints a new one. The page says so next to the result:

> Re-rendered, not edited. The box cannot modify pixels in an uploaded image, so this is a new painting based on what the model saw.

The box has no image-to-image route. Fields named `image`, `mask`, `init_image`, `input_image`, `image_url` and `strength` are accepted on the image endpoints and silently ignored. This app does not send them.

## What it does not

- It does not silently unload a resident model to make room for a Load click. If the one you picked does not fit, the page says what is resident, what it costs, and you unload it yourself.
- It does not paper over Background-Remove or Object-Remove as pixel editors. Those are listed as Text-to-Image and the gateway gives them no way to receive an image.
- It does not add a Mac-side background remover.
- It does not guess request fields per model. When the device refuses, the body of the refusal is what you see.

## Where the files live

| | |
|---|---|
| Gallery | `~/.local/share/tiiny-image-studio/gallery/` |
| Saved address / key | `~/.config/tiiny-image-studio.json` |
| Override gallery root | `TIINY_IMAGE_STUDIO_HOME` |

Each plate is a PNG plus a JSON sidecar with model, prompt, negative prompt, seed, steps, wall time, and parent id when it was derived from an upload.

## Device

Same discovery as TiinyBench: `TIINY_BASE` / `TIINY_KEY`, then `~/.tiinyapps/device.json`, then a saved address, then a scan. Paste an address and a key on the page if none of that is set.
