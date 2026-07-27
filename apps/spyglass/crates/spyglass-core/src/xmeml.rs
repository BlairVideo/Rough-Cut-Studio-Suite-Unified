//! Premiere Pro XMEML v5 export (Section 14): the pool tray's ordered
//! shots become a single sequence, each clip placed back-to-back at its
//! correct in/out points.
//!
//! Ported from -- not imported from, not calling into -- Rough Cut
//! Studio's `xml_builder.py` and B-Roll Analyzer's `xml_export.py`, used
//! purely as reference implementations per Section 19.1's decided "port,
//! don't extract" call. Neither sibling app's files were read as anything
//! but read-only reference, and neither was modified.
//!
//! Load-bearing details ported from those references (confirmed there
//! against a real Premiere import, not just theorized):
//! - Every clipitem's in/out/start/end/duration must be computed at the
//!   *sequence's* frame rate, never a source clip's native rate, even
//!   when they differ -- mixing rates this way produces a Premiere
//!   "insufficient media" error.
//! - Stereo/multichannel audio must be split into one mono clipitem per
//!   channel on separate sequence audio tracks, each pinned via
//!   `<sourcetrack>` -- a single multichannel clipitem silently imports
//!   as mono.
//! - `<link>` elements bind a video clipitem to its paired audio
//!   clipitem(s) so Premiere treats them as one linked selection.
//! - `<file>` elements are deduplicated by real source path: the first
//!   clipitem referencing a given file gets the full `<file>` definition,
//!   every later one just references it by id.
//! - No drop-frame timecode math (neither reference implements it).
//! - `pathurl` is `file://localhost` + an absolute POSIX-style path, with
//!   literal (non-percent-encoded) characters -- only XML-special
//!   characters (`&`/`<`/`>`) are escaped.
//!
//! **Phase 5**: audio format (channel count/sample rate/bit depth) is now
//! each clip's own real probed value (`crate::ffprobe::AudioFormat`)
//! rather than the fixed stereo/48kHz/16-bit assumption Phase 4 shipped
//! with (Section 14's build note) -- still always emitted as an L/R pair
//! of mono clipitems per clip (Section 14's "stereo audio must be split
//! into one mono clipitem per channel" rule), but a clip whose real source
//! is mono now gets a second, silent-but-present audio channel rather than
//! silently claiming stereo it doesn't have.

use crate::ffprobe::AudioFormat;
use std::collections::HashMap;
use std::fmt::Write as _;

/// One pool shot as the exporter needs it -- already resolved from the
/// database, so this module has no SQL dependency of its own and is easy
/// to unit-test in isolation.
#[derive(Debug, Clone, PartialEq)]
pub struct XmemlClip {
    pub file_path: String,
    pub name: String,
    pub frame_rate: Option<f64>,
    pub in_seconds: f64,
    pub out_seconds: f64,
    /// This clip's real probed audio format (Section 15's ffprobe
    /// probing) -- `None` falls back to `AudioFormat::FALLBACK`
    /// (stereo/48kHz/16-bit), the same assumption Phase 4 always made.
    pub audio_format: Option<AudioFormat>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct XmemlOptions {
    pub sequence_name: String,
    pub width: u32,
    pub height: u32,
}

impl Default for XmemlOptions {
    fn default() -> Self {
        XmemlOptions { sequence_name: "Spyglass Pool".to_string(), width: 1920, height: 1080 }
    }
}

/// A conventional broadcast default, used only when not one clip in the
/// pool has a known frame rate.
const FALLBACK_FRAME_RATE: f64 = 29.97;

/// Picks the sequence's own frame rate from its clips' native rates
/// (Section 14: "mixed frame rates... need to be handled per-clip rather
/// than assumed constant") -- the most common rate among the pool's known
/// rates, so the sequence matches the footage that's actually there
/// rather than an assumed constant. Rates are bucketed to the nearest
/// thousandth first so near-identical floats (29.9699997 vs 29.97) count
/// as the same rate.
pub fn pick_sequence_frame_rate(frame_rates: &[Option<f64>]) -> f64 {
    let mut counts: HashMap<i64, (f64, usize)> = HashMap::new();
    for &rate in frame_rates.iter().flatten() {
        let bucket = (rate * 1000.0).round() as i64;
        let entry = counts.entry(bucket).or_insert((rate, 0));
        entry.1 += 1;
    }
    counts
        .into_values()
        .max_by_key(|&(_, count)| count)
        .map(|(rate, _)| rate)
        .unwrap_or(FALLBACK_FRAME_RATE)
}

/// `timebase`/`ntsc` per the reconciled rule from the reference
/// implementations: round the fps to get the timebase; it's an NTSC rate
/// if that rounded value is one of the NTSC-capable bases (24/30/48/60)
/// *and* the original float actually deviated from it (an exact 24.0
/// stays non-NTSC; 23.976 does not).
fn timebase_and_ntsc(fps: f64) -> (i64, bool) {
    let rounded = fps.round() as i64;
    let ntsc = matches!(rounded, 24 | 30 | 48 | 60) && (fps - rounded as f64).abs() > 0.01;
    (rounded, ntsc)
}

fn to_frames(seconds: f64, fps: f64) -> i64 {
    (seconds * fps).round().max(0.0) as i64
}

/// XML-escapes text content/attribute values. Neither reference
/// percent-encodes `pathurl`s, so this deliberately only escapes what XML
/// itself requires.
fn xml_escape(s: &str) -> String {
    s.replace('&', "&amp;").replace('<', "&lt;").replace('>', "&gt;")
}

fn to_pathurl(file_path: &str) -> String {
    let normalized = file_path.replace('\\', "/");
    let normalized = if normalized.starts_with('/') { normalized } else { format!("/{normalized}") };
    format!("file://localhost{normalized}")
}

fn rate_block(indent: &str, fps: f64) -> String {
    let (timebase, ntsc) = timebase_and_ntsc(fps);
    format!(
        "{indent}<rate>\n{indent}  <timebase>{timebase}</timebase>\n{indent}  <ntsc>{}</ntsc>\n{indent}</rate>\n",
        if ntsc { "TRUE" } else { "FALSE" }
    )
}

struct ClipItemIds {
    video: String,
    audio_left: String,
    audio_right: String,
}

/// How many mono audio clipitems/tracks a clip gets: the source's own
/// real channel count (Section 15), clamped to the L/R pair this module's
/// track layout supports -- a mono field-mic source gets one track, not a
/// second silent one claiming stereo it doesn't have; a >2-channel source
/// (rare professional multichannel audio) is treated as stereo, a known,
/// deliberate scope limit rather than a full N-track generalization.
fn audio_channel_count(format: AudioFormat) -> usize {
    match format.channels {
        0 | 1 => 1,
        _ => 2,
    }
}

/// `link_block`, but only for the channels this clip actually has --
/// `channel_count` is 1 or 2 (see `audio_channel_count`).
fn link_block(indent: &str, ids: &ClipItemIds, clip_index: usize, channel_count: usize) -> String {
    let mut out = String::new();
    let mut members: Vec<(&str, &str, i64)> = vec![(&ids.video, "video", 1), (&ids.audio_left, "audio", 1)];
    if channel_count >= 2 {
        members.push((&ids.audio_right, "audio", 2));
    }
    for (linkref, mediatype, trackindex) in members {
        let _ = write!(
            out,
            "{indent}<link>\n{indent}  <linkclipref>{linkref}</linkclipref>\n{indent}  <mediatype>{mediatype}</mediatype>\n{indent}  <trackindex>{trackindex}</trackindex>\n{indent}  <clipindex>{clip_index}</clipindex>\n{indent}</link>\n"
        );
    }
    out
}

/// Builds a complete XMEML v5 document: one bare `<sequence>` (matching
/// Rough Cut Studio's reference shape, not B-Roll Analyzer's fuller
/// `<project>/<bin>` wrapper -- the plan's Section 14 describes just a
/// ready-made sequence, not a browsable bin of master clips) containing
/// the pool's clips back-to-back on one video track plus a stereo
/// (L/R) audio pair.
pub fn build_sequence_xml(clips: &[XmemlClip], options: &XmemlOptions) -> String {
    let sequence_fps = pick_sequence_frame_rate(&clips.iter().map(|c| c.frame_rate).collect::<Vec<_>>());

    let mut file_ids: HashMap<String, String> = HashMap::new();
    let mut video_items = String::new();
    let mut audio_left_items = String::new();
    let mut audio_right_items = String::new();
    let mut timeline_frame: i64 = 0;
    let mut total_frames: i64 = 0;

    for (i, clip) in clips.iter().enumerate() {
        let clip_index = i + 1;
        let in_frames = to_frames(clip.in_seconds, sequence_fps);
        let out_frames = to_frames(clip.out_seconds, sequence_fps).max(in_frames + 1);
        let length = out_frames - in_frames;
        let start = timeline_frame;
        let end = start + length;
        timeline_frame = end;
        total_frames = total_frames.max(end);

        let ids = ClipItemIds {
            video: format!("clipitem-V1-{clip_index}"),
            audio_left: format!("clipitem-AL1-{clip_index}"),
            audio_right: format!("clipitem-AR1-{clip_index}"),
        };

        let is_first_reference = !file_ids.contains_key(&clip.file_path);
        let next_file_number = file_ids.len() + 1;
        let file_id = file_ids
            .entry(clip.file_path.clone())
            .or_insert_with(|| format!("file-{next_file_number}"))
            .clone();

        let audio_format = clip.audio_format.unwrap_or(AudioFormat::FALLBACK);
        let channel_count = audio_channel_count(audio_format);

        let file_block = if is_first_reference {
            format!(
                "            <file id=\"{file_id}\">\n\
                 \x20              <name>{name}</name>\n\
                 \x20              <pathurl>{pathurl}</pathurl>\n\
                 {rate}\
                 \x20              <media>\n\
                 \x20                <video><samplecharacteristics><width>{w}</width><height>{h}</height></samplecharacteristics></video>\n\
                 \x20                <audio><samplecharacteristics><depth>{depth}</depth><samplerate>{samplerate}</samplerate></samplecharacteristics><channelcount>{channelcount}</channelcount></audio>\n\
                 \x20              </media>\n\
                 \x20            </file>\n",
                name = xml_escape(&clip.name),
                pathurl = xml_escape(&to_pathurl(&clip.file_path)),
                rate = rate_block("              ", sequence_fps),
                w = options.width,
                h = options.height,
                depth = audio_format.bit_depth.unwrap_or(16),
                samplerate = audio_format.sample_rate,
                channelcount = channel_count,
            )
        } else {
            format!("            <file id=\"{file_id}\"/>\n")
        };

        let links = link_block("            ", &ids, clip_index, channel_count);

        let _ = write!(
            video_items,
            "          <clipitem id=\"{vid}\">\n\
             \x20            <name>{name}</name>\n\
             \x20            <duration>{length}</duration>\n\
             {rate}\
             \x20            <start>{start}</start>\n\
             \x20            <end>{end}</end>\n\
             \x20            <in>{in_frames}</in>\n\
             \x20            <out>{out_frames}</out>\n\
             {file_block}\
             {links}\
             \x20          </clipitem>\n",
            vid = ids.video,
            name = xml_escape(&clip.name),
            rate = rate_block("            ", sequence_fps),
        );

        // Audio clipitems reference the already-defined file by id only --
        // the full <file> definition lives solely on the video clipitem's
        // first occurrence.
        let audio_file_ref = format!("            <file id=\"{file_id}\"/>\n");

        let mut audio_tracks: Vec<(&mut String, i64, &String)> = vec![(&mut audio_left_items, 1, &ids.audio_left)];
        if channel_count >= 2 {
            audio_tracks.push((&mut audio_right_items, 2, &ids.audio_right));
        }
        for (buf, track_index, clip_id) in audio_tracks {
            let _ = write!(
                buf,
                "          <clipitem id=\"{clip_id}\">\n\
                 \x20            <name>{name}</name>\n\
                 \x20            <duration>{length}</duration>\n\
                 {rate}\
                 \x20            <start>{start}</start>\n\
                 \x20            <end>{end}</end>\n\
                 \x20            <in>{in_frames}</in>\n\
                 \x20            <out>{out_frames}</out>\n\
                 {audio_file_ref}\
                 \x20            <sourcetrack><mediatype>audio</mediatype><trackindex>{track_index}</trackindex></sourcetrack>\n\
                 {links}\
                 \x20          </clipitem>\n",
                name = xml_escape(&clip.name),
                rate = rate_block("            ", sequence_fps),
            );
        }
    }

    let sequence_id = format!("sequence-{}", uuid::Uuid::new_v4().simple());

    format!(
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n\
         <!DOCTYPE xmeml>\n\
         <xmeml version=\"5\">\n\
         \x20 <sequence id=\"{sequence_id}\">\n\
         \x20   <name>{sequence_name}</name>\n\
         \x20   <duration>{total_frames}</duration>\n\
         {seq_rate}\
         \x20   <media>\n\
         \x20     <video>\n\
         \x20       <format>\n\
         \x20         <samplecharacteristics>\n\
         {sample_rate}\
         \x20           <width>{width}</width>\n\
         \x20           <height>{height}</height>\n\
         \x20         </samplecharacteristics>\n\
         \x20       </format>\n\
         \x20       <track>\n\
         {video_items}\
         \x20       </track>\n\
         \x20     </video>\n\
         \x20     <audio>\n\
         \x20       <numOutputChannels>2</numOutputChannels>\n\
         \x20       <format>\n\
         \x20         <samplecharacteristics><depth>16</depth><samplerate>48000</samplerate></samplecharacteristics>\n\
         \x20         <channelcount>2</channelcount>\n\
         \x20       </format>\n\
         \x20       <track>\n\
         {audio_left_items}\
         \x20       </track>\n\
         \x20       <track>\n\
         {audio_right_items}\
         \x20       </track>\n\
         \x20     </audio>\n\
         \x20   </media>\n\
         \x20 </sequence>\n\
         </xmeml>\n",
        sequence_name = xml_escape(&options.sequence_name),
        seq_rate = rate_block("    ", sequence_fps),
        sample_rate = rate_block("            ", sequence_fps),
        width = options.width,
        height = options.height,
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn timebase_and_ntsc_matches_reconciled_reference_rule() {
        assert_eq!(timebase_and_ntsc(23.976), (24, true));
        assert_eq!(timebase_and_ntsc(29.97), (30, true));
        assert_eq!(timebase_and_ntsc(59.94), (60, true));
        assert_eq!(timebase_and_ntsc(24.0), (24, false));
        assert_eq!(timebase_and_ntsc(25.0), (25, false));
        assert_eq!(timebase_and_ntsc(30.0), (30, false));
    }

    #[test]
    fn to_frames_rounds_and_never_goes_negative() {
        assert_eq!(to_frames(1.0, 30.0), 30);
        assert_eq!(to_frames(0.0, 29.97), 0);
        assert_eq!(to_frames(-5.0, 30.0), 0);
    }

    #[test]
    fn pick_sequence_frame_rate_uses_the_most_common_known_rate() {
        let rates = vec![Some(29.97), Some(29.97), Some(23.976)];
        assert!((pick_sequence_frame_rate(&rates) - 29.97).abs() < 1e-6);
    }

    #[test]
    fn pick_sequence_frame_rate_falls_back_when_nothing_is_known() {
        let rates: Vec<Option<f64>> = vec![None, None];
        assert_eq!(pick_sequence_frame_rate(&rates), FALLBACK_FRAME_RATE);
    }

    #[test]
    fn to_pathurl_uses_localhost_and_literal_spaces() {
        assert_eq!(
            to_pathurl("/Volumes/Archive/Fall 2025/clip.mov"),
            "file://localhost/Volumes/Archive/Fall 2025/clip.mov"
        );
    }

    #[test]
    fn xml_escape_handles_ampersand_and_angle_brackets() {
        assert_eq!(xml_escape("Coach & Team <Highlights>"), "Coach &amp; Team &lt;Highlights&gt;");
    }

    fn sample_clip(path: &str, name: &str, fps: f64, in_s: f64, out_s: f64) -> XmemlClip {
        XmemlClip {
            file_path: path.to_string(),
            name: name.to_string(),
            frame_rate: Some(fps),
            in_seconds: in_s,
            out_seconds: out_s,
            audio_format: None,
        }
    }

    fn sample_clip_with_audio(path: &str, name: &str, fps: f64, in_s: f64, out_s: f64, audio: AudioFormat) -> XmemlClip {
        XmemlClip { audio_format: Some(audio), ..sample_clip(path, name, fps, in_s, out_s) }
    }

    #[test]
    fn build_sequence_xml_is_well_formed_and_has_one_clipitem_per_pool_shot() {
        let clips = vec![
            sample_clip("/Volumes/Archive/game1.mov", "game1.mov", 29.97, 10.0, 14.0),
            sample_clip("/Volumes/Archive/game2.mov", "game2.mov", 29.97, 0.0, 2.0),
        ];
        let xml = build_sequence_xml(&clips, &XmemlOptions::default());

        assert!(xml.starts_with("<?xml version=\"1.0\" encoding=\"UTF-8\"?>"));
        assert!(xml.contains("<!DOCTYPE xmeml>"));
        assert!(xml.contains("<xmeml version=\"5\">"));
        assert_eq!(xml.matches("<clipitem id=\"clipitem-V1-").count(), 2);
        assert_eq!(xml.matches("<clipitem id=\"clipitem-AL1-").count(), 2);
        assert_eq!(xml.matches("<clipitem id=\"clipitem-AR1-").count(), 2);
        // 3 links per clipitem (video + audio-L + audio-R) x 3 clipitem kinds x 2 clips = 18
        assert_eq!(xml.matches("<link>").count(), 18);
    }

    #[test]
    fn build_sequence_xml_places_clips_back_to_back_at_sequence_frame_rate() {
        let clips = vec![
            sample_clip("/Volumes/Archive/a.mov", "a.mov", 30.0, 0.0, 4.0), // 120 frames @ 30fps
            sample_clip("/Volumes/Archive/b.mov", "b.mov", 30.0, 10.0, 12.0), // 60 frames @ 30fps
        ];
        let xml = build_sequence_xml(&clips, &XmemlOptions::default());

        // First clip: start 0, end 120, in 0, out 120 (4s @ 30fps).
        for tag in ["<start>0</start>", "<end>120</end>", "<in>0</in>", "<out>120</out>"] {
            assert!(xml.contains(tag), "missing {tag} in:\n{xml}");
        }
        // Second clip starts exactly where the first ends (back-to-back),
        // with its own in/out at 10s-12s @ 30fps = frames 300-360.
        for tag in ["<start>120</start>", "<end>180</end>", "<in>300</in>", "<out>360</out>"] {
            assert!(xml.contains(tag), "missing {tag} in:\n{xml}");
        }
        // Sequence duration covers the full timeline.
        assert!(xml.contains("<duration>180</duration>"));
    }

    /// A hand-templated string is only as good as its tag-nesting
    /// discipline -- this parses the actual output with a real XML reader
    /// (not just substring checks) to catch a mismatched/unclosed tag that
    /// substring assertions alone wouldn't notice.
    fn assert_well_formed_xml(xml: &str) {
        use quick_xml::events::Event;
        use quick_xml::reader::Reader;

        let mut reader = Reader::from_str(xml);
        let mut depth: i32 = 0;
        loop {
            match reader.read_event() {
                Ok(Event::Eof) => break,
                Ok(Event::Start(_)) => depth += 1,
                Ok(Event::End(_)) => depth -= 1,
                Ok(_) => {}
                Err(e) => panic!("XML is not well-formed: {e} in:\n{xml}"),
            }
        }
        assert_eq!(depth, 0, "unbalanced tags (depth {depth} at EOF) in:\n{xml}");
    }

    #[test]
    fn build_sequence_xml_output_is_well_formed_xml() {
        let clips = vec![
            sample_clip("/Volumes/Archive/a.mov", "a.mov", 30.0, 0.0, 4.0),
            sample_clip("/Volumes/Archive/reused.mov", "reused & special <chars>.mov", 23.976, 1.0, 3.0),
            sample_clip("/Volumes/Archive/reused.mov", "reused & special <chars>.mov", 23.976, 5.0, 7.0),
        ];
        let xml = build_sequence_xml(&clips, &XmemlOptions::default());
        assert_well_formed_xml(&xml);
    }

    #[test]
    fn build_sequence_xml_dedupes_file_definitions_by_path() {
        let clips = vec![
            sample_clip("/Volumes/Archive/reused.mov", "reused.mov", 30.0, 0.0, 2.0),
            sample_clip("/Volumes/Archive/reused.mov", "reused.mov", 30.0, 5.0, 7.0),
        ];
        let xml = build_sequence_xml(&clips, &XmemlOptions::default());

        // Only one full <file> definition (with a <pathurl>) for the shared path...
        assert_eq!(xml.matches("<pathurl>").count(), 1);
        // ...but every clipitem still references a file id (full def + 3 bare refs for the second clip's own video+audio pair, plus the first clip's own audio pair referencing it too).
        assert!(xml.matches("file-1").count() >= 2);
    }

    #[test]
    fn build_sequence_xml_escapes_special_characters_in_names_and_paths() {
        let clips = vec![sample_clip("/Volumes/Archive/A & B.mov", "A & B <take 2>", 30.0, 0.0, 1.0)];
        let xml = build_sequence_xml(&clips, &XmemlOptions::default());
        assert!(xml.contains("A &amp; B &lt;take 2&gt;"));
        assert!(xml.contains("file://localhost/Volumes/Archive/A &amp; B.mov"));
    }

    #[test]
    fn build_sequence_xml_with_no_clips_produces_an_empty_but_valid_sequence() {
        let xml = build_sequence_xml(&[], &XmemlOptions::default());
        assert!(xml.contains("<duration>0</duration>"));
        assert!(!xml.contains("<clipitem"));
    }

    #[test]
    fn audio_channel_count_treats_mono_as_one_track_and_anything_else_as_stereo() {
        assert_eq!(audio_channel_count(AudioFormat { channels: 1, sample_rate: 44100, bit_depth: None }), 1);
        assert_eq!(audio_channel_count(AudioFormat { channels: 2, sample_rate: 48000, bit_depth: None }), 2);
        assert_eq!(audio_channel_count(AudioFormat { channels: 6, sample_rate: 48000, bit_depth: None }), 2);
        assert_eq!(audio_channel_count(AudioFormat { channels: 0, sample_rate: 48000, bit_depth: None }), 1);
    }

    #[test]
    fn build_sequence_xml_gives_a_mono_source_only_one_audio_track_and_no_right_channel_link() {
        let clips = vec![sample_clip_with_audio(
            "/Volumes/Archive/interview.mov",
            "interview.mov",
            29.97,
            0.0,
            4.0,
            AudioFormat { channels: 1, sample_rate: 44100, bit_depth: Some(16) },
        )];
        let xml = build_sequence_xml(&clips, &XmemlOptions::default());

        assert_eq!(xml.matches("<clipitem id=\"clipitem-AL1-").count(), 1, "expects one left/mono audio clipitem");
        assert_eq!(xml.matches("<clipitem id=\"clipitem-AR1-").count(), 0, "a mono source must not get a fabricated right channel");
        // 2 physical clipitems (video + audio-L, no audio-R) x a 2-member
        // link block (video+audio-L) each = 4 -- no audio-R link anywhere.
        assert_eq!(xml.matches("<link>").count(), 4);
        assert!(xml.contains("<channelcount>1</channelcount>"));
        assert!(xml.contains("<samplerate>44100</samplerate>"));
        assert_well_formed_xml(&xml);
    }

    #[test]
    fn build_sequence_xml_reflects_each_clips_real_probed_audio_format_not_a_fixed_assumption() {
        let clips = vec![
            sample_clip_with_audio(
                "/Volumes/Archive/camcorder.mov",
                "camcorder.mov",
                29.97,
                0.0,
                2.0,
                AudioFormat { channels: 2, sample_rate: 32000, bit_depth: Some(12) },
            ),
            sample_clip_with_audio(
                "/Volumes/Archive/lav_mic.mov",
                "lav_mic.mov",
                29.97,
                0.0,
                2.0,
                AudioFormat { channels: 1, sample_rate: 48000, bit_depth: Some(24) },
            ),
        ];
        let xml = build_sequence_xml(&clips, &XmemlOptions::default());

        assert!(xml.contains("<samplerate>32000</samplerate>"));
        assert!(xml.contains("<depth>12</depth>"));
        assert!(xml.contains("<samplerate>48000</samplerate>"));
        assert!(xml.contains("<depth>24</depth>"));
    }

    #[test]
    fn build_sequence_xml_falls_back_to_broadcast_default_when_audio_format_is_unknown() {
        let clips = vec![sample_clip("/Volumes/Archive/unprobed.mov", "unprobed.mov", 29.97, 0.0, 2.0)];
        let xml = build_sequence_xml(&clips, &XmemlOptions::default());
        assert!(xml.contains("<channelcount>2</channelcount>"));
        assert!(xml.contains("<samplerate>48000</samplerate>"));
        assert!(xml.contains("<depth>16</depth>"));
    }
}
