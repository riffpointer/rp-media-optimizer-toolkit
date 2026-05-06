# RiffPointer's Media Optimizer Toolkit

Desktop media optimization tools for Godot projects.

The toolkit scans a Godot project for large or high-resolution source media and offers batch conversion flows for images, audio, and video.

## Features

- Image scanning for PNG files in common Godot asset folders.
- Image downscaling and optimization through Pillow.
- Optional `pngquant` and `optipng` support when those tools are available on `PATH`.
- Audio conversion through `ffmpeg`, including codec, bitrate, sample rate, channel, normalization, and silence trimming options.
- Video conversion through `ffmpeg`, including codec, CRF/bitrate, preset, audio, scaling, frame rate, and MP4 faststart options.
- Batch progress with processing rate and estimated remaining time.

## Requirements

- Python 3.10 or newer.
- `ffmpeg` and `ffprobe` on `PATH` for audio and video conversion.
- Optional: `pngquant` and `optipng` on `PATH` for extra PNG compression.

Install Python dependencies from this folder:

```powershell
py -m pip install -r requirements.txt
```

## Run

From the repository root:

```powershell
py ".\RP Media Optimizer Toolkit\rp_media_optimizer_toolkit.py"
```

Or from this folder:

```powershell
py .\rp_godot_optimizer_toolkit.py
```

## License

RP Media Optimizer Toolkit is licensed under the Mozilla Public License 2.0. See `LICENSE`.

Created by RiffPointer.
