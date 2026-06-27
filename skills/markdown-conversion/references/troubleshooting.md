# Troubleshooting

## Garbled Text / Mojibake (Chinese Characters)

**Symptoms:** Output shows `????` or garbled text instead of Chinese characters.

**Cause:** Document encoded in GBK/GB2312. The skill requires `chardet` to detect and fix this automatically (auto-installed if missing).

**Solution:** The skill handles this automatically. If the issue persists, ensure `chardet` is installed:
```bash
pip install chardet
```

**How it works:** Documents with non-UTF-8 encodings (e.g., GBK/GB2312 Chinese text) are automatically detected and converted:
1. Uses `chardet` to detect source encoding (required dependency)
2. Decodes with detected encoding, re-encodes to UTF-8
3. Converts all Traditional Chinese characters to Simplified Chinese via `opencc`

## Legacy .doc Files Not Converting

**Symptoms:** Error "UnsupportedFormatException" for .doc files.

**Solution:** The skill auto-installs `doc2docx` for legacy `.doc` format support. No manual action needed.

**How it works:** MarkItDown only supports modern `.docx` format, not legacy `.doc` (Word 97-2003). The skill handles this automatically:
1. Detects `.doc` file extension
2. Auto-installs `doc2docx` if not present
3. Converts to temporary `.docx` using `doc2docx` library
4. Processes the converted file
5. Cleans up temporary files

**Alternative:** Manually convert using Word or LibreOffice:
```bash
# LibreOffice (if available)
soffice --headless --convert-to docx input.doc
```

## Permission Denied Errors

**Cause:** Windows file locking or insufficient permissions.

**Solutions:**
- Close the file in other applications before converting
- Run with appropriate permissions
- Check if vault folder has write access

## MarkItDown Not Found

**Symptoms:** Error "MarkItDown not found."

**Solution:** The skill auto-installs `markitdown` if missing. If the issue persists:
```bash
pip install markitdown
```

## Large Files Taking Too Long

**Cause:** Conversion of large PDFs or complex documents can take several minutes.

**Solutions:**
- Wait for progress updates - the skill reports each step for large files
- Typical times: 10MB file = 1-3 minutes, 50MB file = 5-10 minutes
- Consider splitting very large PDFs first
- Use `--no-frontmatter` for slightly faster processing

## FFmpeg Warning (Media Files)

**Symptoms:** `RuntimeWarning: Couldn't find ffmpeg or avconv - defaulting to ffmpeg, but may not work`

**Cause:** The `markitdown` tool uses ffmpeg to extract metadata from audio/video files (MP3, MP4, WAV, etc.). This warning appears when ffmpeg is not installed on your system.

**Solution:**

**Windows:**
```bash
# Install via winget
winget install Gyan.FFmpeg

# Or download from https://ffmpeg.org/download.html and add to PATH
```

**macOS:**
```bash
brew install ffmpeg
```

**Linux:**
```bash
sudo apt-get install ffmpeg  # Debian/Ubuntu
sudo yum install ffmpeg      # RHEL/CentOS
```

**Note:** This warning is harmless for document conversion (PDF, DOCX, etc.). It only affects media file metadata extraction.

## Error Reference

Common error scenarios and how the skill handles them:

| Scenario | Behavior |
|----------|----------|
| File not found | Clear error: `"File not found: {path}"` |
| Unsupported format | Error with extension, suggest checking markitdown docs |
| MarkItDown not installed | Auto-installed by pipeline |
| Vault path doesn't exist | Error: `"Vault not found at {path}"` |
| Output file exists | Prompt to overwrite, rename, or cancel |
| Permission denied | Error: `"Permission denied writing to {path}"` |
| Conversion fails | Error with markitdown's error message |
| Legacy .doc without doc2docx | Auto-installed by pipeline |
| URL fetch fails | Error from markitdown's HTTP client |

## Limitations

| Limitation | Details | Workaround |
|------------|---------|------------|
| **Images** | Embedded images are stripped by default | Use `--keep-images` to preserve image links |
| **Read-only source directory** | Default output writes next to the source and fails if that directory is not writable | Pass `--output-path` pointing to a writable location |
| **Formatting** | Complex formatting may be simplified | Review and adjust markdown after conversion |
| **Password-protected files** | Cannot convert encrypted documents | Remove password protection first |
| **Very large files** | Files > 100MB may take significant time | Split PDFs or process in batches |
| **Internal links** | Document links are not converted to wikilinks | Manually convert `[]()` to `[[]]` syntax |
