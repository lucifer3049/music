@echo off
rem ============================================================
rem Personal settings template.
rem
rem ASCII only, CRLF line endings -- this file is `call`ed by start.bat,
rem so the same cmd.exe parsing rules apply. Non-ASCII text here will
rem break the launcher.
rem
rem Usage: copy this file to settings.local.cmd, then edit that copy.
rem settings.local.cmd is not tracked by git, so updates will not
rem overwrite your settings. Remove the leading "rem" to enable a line.
rem ============================================================

rem MusicBrainz requires a contact address in the User-Agent, otherwise it
rem may refuse requests. The symptom is every candidate showing as unmatched.
rem Put your own email here.
rem set MUSICBRAINZ_CONTACT=your-email@example.com

rem Music library root. The m4a files land here, for players and media
rem servers to scan.
rem set MUSIC_ROOT=D:\Music

rem Archive root. The opus files land here. Do not let players scan this
rem tree as well, or every track shows up twice in your library.
rem set ARCHIVE_ROOT=D:\Archive

rem Keep a second copy of every track as .opus under ARCHIVE_ROOT.
rem Off by default: a live measurement found opus at ~129 kbps against m4a at
rem ~128 kbps, so the source bitrate is not reliably higher and the second copy
rem roughly doubles storage for no real gain. Turning this on also doubles
rem download time, since both streams have to be fetched.
rem set KEEP_ARCHIVE_COPY=1

rem Port for the web interface. Default is 8899. Only change this if it
rem clashes with something else; start.bat already searches forward when
rem the port is busy.
rem set MUSIC_DOWNLOADER_PORT=8899
