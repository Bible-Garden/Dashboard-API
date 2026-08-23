# Downloading Audio (MP3)

Audio files are stored in the following structure:

```
<MP3_FILES_PATH>/<translation_alias>/<voice_alias>/mp3/<book_zerofill>/<chapter_zerofill>.mp3
```

Where `link_template` is taken from the DB (`voices.link_template`) and placeholders are filled similarly to `/root/cep/bible-parser/include.php:get_chapter_audio_url`.

Download script: `scripts/download_audio.py`.

It downloads mp3 files for all active voices from the DB:
- `voices.active = 1`
- `translations.active = 1`

Run (inside the `bible-api` container):

```bash
# preview what will be downloaded (without downloading)
docker exec bible-api python3 /code/scripts/download_audio.py --dry-run

# download everything (can be tens of GB)
docker exec bible-api python3 /code/scripts/download_audio.py --yes --max-workers 8

# download only one translation/voice
docker exec bible-api python3 /code/scripts/download_audio.py --yes --translation-alias syn --voice-alias bondarenko

# NPU (npu/npu_uk): source is open.bible, audio is in ZIP archives per book
docker exec bible-api python3 /code/scripts/download_audio.py --yes --translation-alias npu --voice-alias npu_uk
```

By default, files are written to `MP3_FILES_PATH` (usually `/audio` inside the container).
