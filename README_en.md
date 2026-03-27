# human_2_KKS_pipeline

GUI tool for this flow:
`Audio/External text -> Transcribe -> Text conversion -> Grok/SBV2 -> KKS event send`

## Quick Start

1. Run `setup.bat` (first time only, may take time).
2. Run `launch.bat`.
3. In the GUI, turn `KKS running` ON.
4. Press `Start`.

Use `Pause` to temporarily stop processing and `Stop` to fully stop.

## Main UI Areas

### Top Area
- `KKS running` toggle
- `Start / Pause / Stop`
- Manual text send box (`Send`)

### Recording Settings tab
- Input device
- WAV folder
- Threshold / silence stop seconds
- Minimum duration
- Pre-roll / post-roll

### Pipeline Settings tab
- KKS folder
- Output folder
- Save options
- FasterWhisper settings
- Grok/TTS Python
- SBV2 folder / model / server URL
- Event send settings (pipe, host, port, endpoint, token)
- Subtitle send settings
- External text receive settings

### Conversion Dictionary tab
- Rule table for normal text conversion

### Transcribe Conversion tab
- Rule table for conversion right after transcription

### Selenium tab
- Chrome launch / connect / close
- Test open for Grok

### Filter tab
- Phrase list to filter out

## source_mode

- `external`: external text only (WAV inputs are ignored)
- `mic`: mic WAV pipeline only (external receiver is not started)
- `both`: both inputs are accepted

## Save Behavior (Default)

Default is set to keep only text outputs:

- `Record WAV`: OFF
- `SBV2 Audio`: OFF
- `Transcript txt`: ON
- `SBV2 txt`: ON

So, audio artifacts are cleaned up after use by default.

## Output Structure

Under the configured output folder:

- `transcripts/` : transcript text files
- `results/` : run result JSON
- `grok_tts_outputs/` : SBV2/Grok intermediate outputs (cleaned depending on save options)

## Config File

- Path: `config.json` in this folder
- Auto-saved on setting changes
- Edit with UTF-8 if you modify manually

## Common Checks

- No reaction: confirm `KKS running` is ON.
- External text not received: check `source_mode` is not `mic`.
- Audio files not left: confirm save options for audio are ON if you want to keep them.
