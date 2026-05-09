# RiffPointer's Media Optimization Toolkit

Media optimization tools for Godot projects (btw it can be used with other engines, or no engine at all).

The toolkit scans a Godot project (i.e a directory) for large or high-resolution source media and offers batch conversion flows for images, audio, and video.

## Note
Currently only tested on **Windows**. Feel free to test it on **Linux** and report any bugs here! (Though I am not responsible for deleted or broken files, I'm warning you, this is not production grade software and shouldn't be used on _VERY IMPORTANT_ data)

## Screenshits

> I know the UI doesnt look _that_ good but I have made sure to fix inconsistencies or annoying bugs in the UI as much as I was able to find them. But if you don't like something or you find a UX issue, please let me know about it by opening an issue!

- Main screen
<img width="1042" height="712" alt="main_screen" src="https://github.com/user-attachments/assets/5382ab89-799f-4d56-bf5f-3c156e3b4ba7" />
<br>

- Convert dialog
<img width="1038" height="709" alt="convert_dialog" src="https://github.com/user-attachments/assets/d5a85a4a-1117-41c2-bd07-e4fbd60f4533" />
<br>

- Image previewer
<img width="942" height="752" alt="image_preview" src="https://github.com/user-attachments/assets/66210920-7017-4c83-9b7a-5f5c07fbdde7" />
<br>

- Audio previewer
<img width="602" height="192" alt="audio_preview" src="https://github.com/user-attachments/assets/e561ad4f-0a3f-458a-9d6b-7917fbaab2a8" />
<br>

- There's also a Video Previewer but I forgot to take screenshit of it :P

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
py .\main.py
```

## License

Licensed under the Mozilla Public License 2.0. See [LICENSE](https://github.com/riffpointer/rp-media-optimizer-toolkit/blob/master/LICENSE).
