## ADDED Requirements

### Requirement: Video content extraction
The system SHALL extract content from video files by generating keyframe images at regular intervals and transcribing the audio track.

#### Scenario: Video with speech and visual content
- **WHEN** a Slack message contains a .mp4 video attachment of a product demo
- **THEN** the system SHALL extract keyframes (1 per 30 seconds), describe them via vision API, transcribe the audio via speech-to-text, and combine the output as enriched message text for downstream extraction

#### Scenario: Silent video (no audio track)
- **WHEN** a video file has no audio track
- **THEN** the system SHALL extract and describe keyframes only, without attempting audio transcription

#### Scenario: Video exceeds size/duration limit
- **WHEN** a video exceeds the configurable maximum duration (default 10 minutes) or file size (default 100MB)
- **THEN** the system SHALL process only the first N minutes/bytes and append a "[truncated]" indicator to the output

### Requirement: Office document text extraction
The system SHALL extract text content from Microsoft Office documents (.docx, .xlsx, .pptx).

#### Scenario: Word document extraction
- **WHEN** a .docx file is attached to a Slack message
- **THEN** the system SHALL extract all paragraph text, preserving heading structure, and feed it as enriched message text (up to configurable char limit, default 10000)

#### Scenario: Excel spreadsheet extraction
- **WHEN** a .xlsx file is attached
- **THEN** the system SHALL extract sheet names and cell text content, formatted as "Sheet: <name>\n<cell contents>" for each sheet

#### Scenario: PowerPoint extraction
- **WHEN** a .pptx file is attached
- **THEN** the system SHALL extract slide text and speaker notes, formatted as "Slide N: <text>\nNotes: <notes>" for each slide

### Requirement: Audio file transcription
The system SHALL transcribe audio files (.mp3, .wav, .m4a, .ogg) attached to messages using a speech-to-text API.

#### Scenario: Audio message transcription
- **WHEN** a Slack message contains an audio recording attachment
- **THEN** the system SHALL transcribe the audio and append the transcript as enriched message text

#### Scenario: Audio exceeds duration limit
- **WHEN** an audio file exceeds the configurable maximum duration (default 30 minutes)
- **THEN** the system SHALL transcribe only the first N minutes and append a "[truncated]" indicator

### Requirement: Media extractor registry pattern
The system SHALL use a registry of media extractors keyed by MIME type, replacing hardcoded if/else branches in MediaProcessor.

#### Scenario: Known MIME type dispatched to correct extractor
- **WHEN** a file with MIME type "application/vnd.openxmlformats-officedocument.wordprocessingml.document" is encountered
- **THEN** the system SHALL dispatch it to the OfficeExtractor (docx handler)

#### Scenario: Unknown MIME type fallback
- **WHEN** a file with an unregistered MIME type is encountered
- **THEN** the system SHALL fall back to metadata-only extraction (filename, size, type) as today

### Requirement: Asynchronous media processing
The system SHALL process video and audio media asynchronously to avoid blocking the main ingestion pipeline.

#### Scenario: Long video does not block batch
- **WHEN** a batch contains a 5-minute video alongside 50 text messages
- **THEN** the text messages SHALL proceed through the pipeline without waiting for video processing to complete; video-derived facts SHALL be persisted when processing finishes
