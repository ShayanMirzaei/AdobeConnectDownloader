"""AdobeConnectDownloader — download Adobe Connect recordings to MP4.

Package layout:
  core/   protocol (auth, gateway, discovery, parallel download)
  media/  FLV reconstruction, ffmpeg composition, whiteboard rendering
  jobs/   resumable job manifest + on-disk chunk store
  ui/     local web UI + server

See docs/PROTOCOL.md for how the recording protocol works.
"""
__version__ = "0.1.0.dev0"
