//! Watched-root scanner -- Phase 1 scope only (Section 17): recursively
//! discover media files under an approved root and register a bare `clips`
//! row for each, deduped by path. Shot detection, embeddings, and gap-fill
//! tagging come later (Phase 2/3) and build on top of these rows.

use crate::db::{self, Db};
use crate::models::{AccessLevel, NewClip, SourceApp, WatchedRoot};
use chrono::{DateTime, Utc};
use std::collections::HashSet;
use std::io::Read;
use std::path::{Path, PathBuf};
use std::time::Duration;
use walkdir::WalkDir;

/// Extension allowlist a "Watched Folders" settings panel would default to
/// (Section 7) -- case-insensitive.
pub const DEFAULT_EXTENSIONS: &[&str] = &["mov", "mp4", "mxf", "m4v", "avi", "mts", "m2ts"];

/// Folder names skipped by default regardless of case, alongside hidden
/// files/folders and the two known sidecar filenames (Section 7).
const EXCLUDED_DIR_NAMES: &[&str] = &["proxies", "proxy", "render", "renders", "cache"];

fn is_hidden(name: &str) -> bool {
    name.starts_with('.')
}

fn is_excluded_dir(name: &str) -> bool {
    is_hidden(name) || EXCLUDED_DIR_NAMES.contains(&name.to_lowercase().as_str())
}

fn is_sidecar_file(name: &str) -> bool {
    name.ends_with(crate::adapters::IVT_CACHE_SUFFIX) || name == crate::adapters::BROLL_CACHE_FILENAME
}

fn has_allowed_extension(path: &Path, extensions: &[String]) -> bool {
    path.extension()
        .and_then(|e| e.to_str())
        .map(|ext| extensions.iter().any(|allowed| allowed.eq_ignore_ascii_case(ext)))
        .unwrap_or(false)
}

/// True if `path` is a Finder alias (the "Make Alias" bookmark-data file,
/// distinct from a Unix symlink -- `follow_links(true)` on `WalkDir` has no
/// effect on these, since the file itself is `S_IFREG`, not `S_IFLNK`).
/// Checked via the `com.apple.FinderInfo` xattr's Finder-flags field
/// (`kIsAlias`, bit `0x8000` of the big-endian u16 at byte offset 8) rather
/// than shelling out per file -- a watched root can hold thousands of
/// ordinary clips, and this needs to run against every one of them, so it
/// has to be a cheap syscall-level check. Confirmed against a real
/// Finder-made alias file: its `FinderInfo` xattr is `66 64 72 70 4D 41 43
/// 53 80 00 ...` -- `80 00` at offset 8 is exactly this flag.
#[cfg(target_os = "macos")]
fn is_finder_alias(path: &Path) -> bool {
    match xattr::get(path, "com.apple.FinderInfo") {
        Ok(Some(data)) if data.len() >= 10 => u16::from_be_bytes([data[8], data[9]]) & 0x8000 != 0,
        _ => false,
    }
}

#[cfg(not(target_os = "macos"))]
fn is_finder_alias(_path: &Path) -> bool {
    false
}

/// Resolves a confirmed Finder alias to its target, via Finder's own
/// "original item" -- the same resolution Finder itself uses when you
/// double-click the alias, so it tracks a moved/renamed target by volume +
/// inode rather than by whatever stale path was bookmarked at creation
/// time. There's no public Foundation/CoreFoundation entry point for this
/// that isn't either deprecated Carbon (`FSResolveAliasFileWithMountFlags`)
/// or requires ObjC bridging; going through Finder via `osascript` is the
/// supported, local (no network) mechanism, and it's only invoked for
/// files `is_finder_alias` already confirmed -- not per ordinary clip.
/// Coercing the resolved item to `alias` before reading its POSIX path is
/// required: asking Finder for `POSIX path of (original item of ...)`
/// directly errors (-1728) when the original item is a folder.
#[cfg(target_os = "macos")]
fn resolve_finder_alias(path: &Path) -> Option<PathBuf> {
    use std::io::Write;
    use std::process::{Command, Stdio};

    let path_str = path.to_str()?;
    let escaped = path_str.replace('\\', "\\\\").replace('"', "\\\"");
    let script = format!(
        "tell application \"Finder\"\n\
         set theItem to original item of (POSIX file \"{escaped}\" as alias)\n\
         set theAlias to theItem as alias\n\
         return POSIX path of theAlias\n\
         end tell"
    );

    let mut child = match Command::new("osascript")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
    {
        Ok(child) => child,
        Err(e) => {
            eprintln!("spyglass: failed to spawn osascript to resolve Finder alias {path_str}: {e}");
            return None;
        }
    };
    if let Some(mut stdin) = child.stdin.take() {
        let _ = stdin.write_all(script.as_bytes());
    }
    let output = match child.wait_with_output() {
        Ok(output) => output,
        Err(e) => {
            eprintln!("spyglass: failed to read osascript output resolving Finder alias {path_str}: {e}");
            return None;
        }
    };
    if !output.status.success() {
        // The likely real-world case: macOS hasn't granted Spyglass
        // Automation permission to control Finder (System Settings ->
        // Privacy & Security -> Automation), or the usage-description key
        // that gates the permission prompt is missing from the app's
        // Info.plist. Either way this fails silently to the user with no
        // OS-level error dialog, so surface it here -- without this the
        // alias is just invisible to the scanner with no clue why.
        let stderr = String::from_utf8_lossy(&output.stderr);
        eprintln!(
            "spyglass: osascript could not resolve Finder alias {path_str} (likely missing Automation permission for Finder): {}",
            stderr.trim()
        );
        return None;
    }

    let text = String::from_utf8(output.stdout).ok()?;
    let trimmed = text.trim().trim_end_matches('/');
    if trimmed.is_empty() {
        None
    } else {
        Some(PathBuf::from(trimmed))
    }
}

#[cfg(not(target_os = "macos"))]
fn resolve_finder_alias(_path: &Path) -> Option<PathBuf> {
    None
}

/// True if `path` is the same as, or falls inside, one of `roots`. A real
/// path-boundary check, not a bare string-prefix test: `/Volumes/Archive`
/// must not swallow a sibling like `/Volumes/Archive2` just because it
/// shares a text prefix. `pub(crate)` so `db::effectively_removed_
/// watched_root_paths` can reuse the same boundary check to test a
/// *removed* root's path against currently-*active* roots.
pub(crate) fn path_is_under_any(path: &str, roots: &[String]) -> bool {
    roots.iter().any(|root| {
        let root = root.trim_end_matches('/');
        path == root || path.starts_with(&format!("{root}/"))
    })
}

/// True if `path` is the same as, or falls inside, one of `removed_roots`.
/// `pub` so every clip-registration path (Card Eater sync, Transcriber/
/// B-Roll sidecar imports) can guard against resurrecting a file the user
/// explicitly removed, not just the watched-folder scanner. Callers should
/// pass `db::effectively_removed_watched_root_paths`, not the raw removed
/// list -- see that function's doc comment for why.
pub fn is_under_a_removed_root(path: &str, removed_roots: &[String]) -> bool {
    path_is_under_any(path, removed_roots)
}

/// Walks `root`, skipping hidden/render-proxy subfolders and the known
/// sidecar filenames, returning every file whose extension is allowlisted.
///
/// `follow_links(true)`: a watched root's *own* path is always walked
/// regardless of this setting (confirmed against walkdir 2.5.0), but a
/// symlink placed *inside* an already-watched folder -- e.g. a shortcut out
/// to a second drive without adding it as its own root -- is a separate
/// entry encountered mid-walk, and without this it's visited as a leaf and
/// never descended into, silently hiding everything behind it. walkdir's
/// own cycle detection (via `same_file`) catches a symlink that loops back
/// to an ancestor directory and yields an `Err` for just that entry rather
/// than recursing forever, which the `filter_map(|e| e.ok())` below already
/// drops -- no dedicated loop guard needed for that case.
///
/// A **Finder alias** (as opposed to a symlink) is a different animal:
/// `follow_links` has no effect on it at all, since it's an ordinary
/// regular file containing bookmark data, not a filesystem-level link --
/// see `is_finder_alias`/`resolve_finder_alias`. One is handled as its own
/// pass per directory: confirmed aliases are pulled out of the walk here
/// and queued in `alias_targets`, then recursed into after the walk
/// finishes, through `discover_media_files_into`'s `visited` set (keyed on
/// canonicalized real paths) guarding against an alias cycle the way
/// walkdir's own `same_file` check guards symlinks.
///
/// A directory alias's own path (as encountered under `root`) is recorded
/// as an `AliasLink` alongside its resolved target -- the folder tree and
/// `folder_path` search filter (`crate::folders`) both derive "what's
/// under this watched root" purely from `clips.file_path` string prefixes,
/// which breaks the moment a Finder alias redirects part of the tree to a
/// real location with no path relationship to where the alias sits (e.g.
/// a different volume entirely). Without recording that redirection here,
/// at the exact point it's resolved, that subtree's clips would be
/// indexed and searchable but permanently unreachable from either
/// feature. A file-target alias isn't recorded -- it registers directly
/// as a clip under its already-resolved real path and has no folder
/// subtree of its own to redirect browsing into.
pub fn discover_media_files(root: &Path, extensions: &[String]) -> (Vec<PathBuf>, Vec<AliasLink>) {
    let mut visited = HashSet::new();
    let mut out = Vec::new();
    let mut alias_links = Vec::new();
    discover_media_files_into(root, extensions, &mut visited, &mut out, &mut alias_links);
    (out, alias_links)
}

/// One Finder-alias boundary crossing found during a scan: `apparent_path`
/// is the alias's own path (as encountered under a watched root, or under
/// another alias's already-resolved target, for a nested/chained alias);
/// `real_path` is where it actually resolves. Persisted via
/// `db::upsert_alias_link` so `folders`'s tree/filter translation helpers
/// can turn an apparent browse/filter path into the real path prefix its
/// clips are actually registered under.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AliasLink {
    pub apparent_path: PathBuf,
    pub real_path: PathBuf,
}

fn discover_media_files_into(
    root: &Path,
    extensions: &[String],
    visited: &mut HashSet<PathBuf>,
    out: &mut Vec<PathBuf>,
    alias_links: &mut Vec<AliasLink>,
) {
    if let Ok(real) = std::fs::canonicalize(root) {
        if !visited.insert(real) {
            return;
        }
    }

    let mut alias_targets: Vec<AliasLink> = Vec::new();
    let mut alias_file_targets: Vec<PathBuf> = Vec::new();

    let files = WalkDir::new(root)
        .follow_links(true)
        .into_iter()
        .filter_entry(|e| {
            if e.file_type().is_dir() && e.depth() > 0 {
                !is_excluded_dir(&e.file_name().to_string_lossy())
            } else {
                true
            }
        })
        .filter_map(|e| e.ok())
        .filter(|e| e.file_type().is_file())
        .filter(|e| {
            let name = e.file_name().to_string_lossy();
            !is_hidden(&name) && !is_sidecar_file(&name)
        })
        .filter(|e| {
            if !is_finder_alias(e.path()) {
                return true;
            }
            // Handled via the alias_targets/recursion path below instead of
            // the extension-filtered file list -- an alias to a folder has
            // no extension of its own to match, and an alias to a file
            // still needs its *target's* extension checked, not the
            // alias's own name.
            if let Some(target) = resolve_finder_alias(e.path()) {
                if target.is_dir() {
                    alias_targets.push(AliasLink { apparent_path: e.path().to_path_buf(), real_path: target });
                } else if has_allowed_extension(&target, extensions) {
                    alias_file_targets.push(target);
                }
            }
            false
        })
        .map(|e| e.into_path())
        .filter(|p| has_allowed_extension(p, extensions));

    out.extend(files);
    out.extend(alias_file_targets);

    for link in alias_targets {
        discover_media_files_into(&link.real_path, extensions, visited, out, alias_links);
        alias_links.push(link);
    }
}

/// BLAKE3 checksum, computed by streaming fixed-size chunks rather than
/// loading the whole (potentially multi-gigabyte) file into memory --
/// Section 2's media-processing standard.
///
/// Deliberately *not* memory-mapped: watched roots here live on spinning
/// working/archive drives, and mmap has two real problems on that hardware.
/// First, BLAKE3's own multi-threaded mmap hasher parallelizes page-faults
/// out of sequential order, which thrashes a spinning disk's seek time
/// instead of helping -- HDD sequential throughput (~150-250MB/s) is well
/// under what even single-threaded hashing can keep up with, so there's no
/// real win to parallelize here in the first place. Second, and more
/// importantly, a disconnected/sleeping/failing external drive turns a
/// mmap'd read into a process-crashing `SIGBUS` instead of a clean `Result`
/// error -- undermining the volume-watcher/cancel-token handling the rest of
/// the app relies on for exactly that failure mode.
pub fn compute_checksum(path: &Path) -> std::io::Result<String> {
    let mut file = std::fs::File::open(path)?;
    let mut hasher = blake3::Hasher::new();
    let mut buf = [0u8; 1024 * 1024];
    loop {
        let n = file.read(&mut buf)?;
        if n == 0 {
            break;
        }
        hasher.update(&buf[..n]);
    }
    Ok(hasher.finalize().to_hex().to_string())
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct ScanStats {
    pub discovered: u64,
    pub registered: u64,
    pub already_registered: u64,
    /// Discovered but skipped because the file falls under a watched root
    /// the user explicitly removed -- see `is_under_a_removed_root`. Not
    /// folded into `already_registered`, since that means something
    /// different (already tracked and fine); this means "was deliberately
    /// purged and must stay that way."
    pub excluded_removed: u64,
    /// Recognized as content already indexed under a now-gone path (an
    /// archive-drive move) and repointed to its new location in place --
    /// see `db::find_clip_by_checksum`. Not folded into `registered`, since
    /// no new clip row (or gap-fill job) was created; every existing shot/
    /// embedding/tag/pool entry carried forward untouched.
    pub relinked: u64,
}

/// Registers every discovered media file under `root_path` into `clips`
/// (Phase 1's "at minimum registers every discovered file" -- Section 17).
/// Dedup is by unique `file_path`; a file whose checksum matches an
/// existing clip whose old path no longer exists on disk is recognized as
/// the same content having moved (e.g. an annual working-drive-to-archive-
/// drive migration) and relinked in place rather than registered as a new,
/// unrelated clip -- see `db::find_clip_by_checksum`. The old-path-gone
/// check is what distinguishes an actual move from someone keeping a
/// duplicate copy on both drives, which must stay two separate clips.
///
/// Takes `db` (the shared `Mutex<Connection>` wrapper) rather than an
/// already-locked `Connection` so it can lock only for the quick per-file
/// DB operations, not for the loop's entire duration. A newly discovered
/// file's BLAKE3 checksum can mean reading multiple gigabytes off a slow
/// external drive; holding the app's single shared connection lock for
/// that would stall every other command needing the database (search,
/// the gap-fill worker, another "Scan now") for the whole scan, not just
/// the caller of this function.
///
/// Each newly registered clip is queued for gap-fill immediately (rather
/// than waiting for `rescan_root`'s single end-of-scan sweep) -- a watched
/// root can hold a handful of ordinary clips alongside a few genuinely
/// massive ones (a multi-camera event master can run tens of GB), and
/// without this, gap-fill for everything the scan had *already* found sat
/// blocked behind however long the walk still had left, however many
/// giant files were still ahead of it.
pub fn scan_and_register(db: &Db, root_path: &Path, extensions: &[String]) -> rusqlite::Result<ScanStats> {
    let (removed_roots, registered_paths) = {
        let conn = db.conn.lock().unwrap();
        (
            db::effectively_removed_watched_root_paths(&conn)?,
            db::registered_clip_paths(&conn)?,
        )
    };

    let (discovered_files, alias_links) = discover_media_files(root_path, extensions);

    if !alias_links.is_empty() {
        let conn = db.conn.lock().unwrap();
        for link in &alias_links {
            db::upsert_alias_link(
                &conn,
                &link.apparent_path.to_string_lossy(),
                &link.real_path.to_string_lossy(),
            )?;
        }
    }

    let mut stats = ScanStats::default();
    for path in discovered_files {
        stats.discovered += 1;
        let path_str = path.to_string_lossy().into_owned();

        if is_under_a_removed_root(&path_str, &removed_roots) {
            stats.excluded_removed += 1;
            continue;
        }

        // In-memory set lookup snapshotted at the start of this scan rather
        // than a fresh per-file query -- see `db::registered_clip_paths`. A
        // path this snapshot missed because a concurrent scan of a
        // *different* root registered it moments ago just falls through to
        // `upsert_clip`'s own `ON CONFLICT(file_path) DO NOTHING`, so
        // staleness costs at most one redundant checksum, never a duplicate
        // row.
        if registered_paths.contains(&path_str) {
            stats.already_registered += 1;
            continue;
        }

        // Deliberately outside the lock -- see doc comment above.
        let size_bytes = std::fs::metadata(&path).ok().map(|m| m.len() as i64);
        let checksum = compute_checksum(&path).ok();

        let conn = db.conn.lock().unwrap();

        if let Some(checksum) = checksum.as_deref() {
            if let Some(existing) = db::find_clip_by_checksum(&conn, checksum)? {
                if existing.file_path != path_str && !Path::new(&existing.file_path).exists() {
                    db::relink_clip_path(&conn, existing.id, &path_str)?;
                    stats.relinked += 1;
                    continue;
                }
            }
        }

        // One transaction for both writes instead of two autocommit
        // statements -- halves the fsyncs a large initial scan does (one
        // clip-row insert + one job-queue insert, previously each its own
        // commit) and makes the pair atomic, so a clip row is never left
        // registered without its gap-fill job queued.
        let tx = conn.unchecked_transaction()?;
        let clip = db::upsert_clip(
            &tx,
            &NewClip {
                file_path: path_str,
                source_app: SourceApp::SpyglassScan,
                checksum,
                size_bytes,
                duration_sec: None,
            },
        )?;
        db::enqueue_gap_fill_job_for_clip(&tx, clip.id)?;
        tx.commit()?;
        stats.registered += 1;
    }
    Ok(stats)
}

/// Re-scans one watched root end to end: registers any newly discovered
/// files, records that the scan happened, and queues gap-fill for anything
/// still pending. This is the single sequence both the manual "Scan now"
/// command and the periodic rescan scheduler (Section 7/17) call, so a
/// scheduled rescan can never drift from what the button does.
pub fn rescan_root(db: &Db, root: &WatchedRoot, extensions: &[String]) -> rusqlite::Result<ScanStats> {
    let stats = scan_and_register(db, Path::new(&root.path), extensions)?;
    let conn = db.conn.lock().unwrap();
    db::touch_watched_root_scanned_at(&conn, root.id)?;
    db::enqueue_pending_gap_fill_jobs(&conn)?;
    Ok(stats)
}

/// Pure filter over already-fetched watched roots: which ones are due for
/// another periodic rescan (Section 7: "periodically re-walked to catch
/// newly added footage"). A root is due if it's `active` and its last scan
/// is missing, unparseable, or older than `interval`. Takes `now` as a
/// parameter rather than reading the clock itself so it's deterministically
/// testable.
pub fn roots_due_for_rescan<'a>(
    roots: &'a [WatchedRoot],
    now: DateTime<Utc>,
    interval: Duration,
) -> Vec<&'a WatchedRoot> {
    roots
        .iter()
        .filter(|root| AccessLevel::from_str(&root.access_level) == AccessLevel::Active)
        .filter(|root| match &root.last_scanned_at {
            None => true,
            Some(ts) => match DateTime::parse_from_rfc3339(ts) {
                Ok(last) => now.signed_duration_since(last) >= chrono::Duration::from_std(interval).unwrap_or_default(),
                Err(_) => true,
            },
        })
        .collect()
}

#[cfg(test)]
fn default_extensions_owned() -> Vec<String> {
    DEFAULT_EXTENSIONS.iter().map(|s| s.to_string()).collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::db::Db;
    use std::sync::atomic::{AtomicU64, Ordering};

    static TMP_COUNTER: AtomicU64 = AtomicU64::new(0);

    fn scratch_dir(tag: &str) -> PathBuf {
        let n = TMP_COUNTER.fetch_add(1, Ordering::SeqCst);
        let dir = std::env::temp_dir().join(format!("spyglass_scanner_test_{tag}_{n}"));
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn discover_media_files_respects_extension_allowlist_and_exclusions() {
        let dir = scratch_dir("discover");
        std::fs::write(dir.join("clip1.mov"), b"data").unwrap();
        std::fs::write(dir.join("notes.txt"), b"data").unwrap();
        std::fs::write(dir.join(".hidden.mov"), b"data").unwrap();
        std::fs::write(dir.join("clip1.mov.ivt-cache.json"), b"{}").unwrap();
        std::fs::create_dir_all(dir.join("proxies")).unwrap();
        std::fs::write(dir.join("proxies").join("clip1_proxy.mov"), b"data").unwrap();
        std::fs::create_dir_all(dir.join("Fall2025")).unwrap();
        std::fs::write(dir.join("Fall2025").join("clip2.mp4"), b"data").unwrap();

        let extensions = default_extensions_owned();
        let (found, alias_links) = discover_media_files(&dir, &extensions);
        let names: Vec<String> = found
            .iter()
            .map(|p| p.file_name().unwrap().to_string_lossy().into_owned())
            .collect();

        assert!(alias_links.is_empty(), "no Finder aliases involved in this fixture");
        assert!(names.contains(&"clip1.mov".to_string()));
        assert!(names.contains(&"clip2.mp4".to_string()));
        assert!(!names.contains(&"notes.txt".to_string()));
        assert!(!names.contains(&".hidden.mov".to_string()));
        assert!(!names.iter().any(|n| n.contains("proxy")));
        assert_eq!(names.len(), 2);

        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn discover_media_files_follows_a_symlinked_subfolder_inside_the_watched_root() {
        // The alias/symlink-shortcut case: a watched root contains a Finder
        // alias or symlink out to a second drive instead of that drive being
        // added as its own root. The root's own path is walked either way
        // (walkdir always follows the root argument), so this only exercises
        // a symlink *encountered during* the walk, one level down.
        let dir = scratch_dir("symlink_subfolder");
        std::fs::write(dir.join("direct_clip.mov"), b"data").unwrap();
        let outside = scratch_dir("symlink_target");
        std::fs::write(outside.join("linked_clip.mov"), b"data").unwrap();
        std::os::unix::fs::symlink(&outside, dir.join("link_to_outside")).unwrap();

        let extensions = default_extensions_owned();
        let (found, alias_links) = discover_media_files(&dir, &extensions);
        let names: Vec<String> = found
            .iter()
            .map(|p| p.file_name().unwrap().to_string_lossy().into_owned())
            .collect();

        assert!(alias_links.is_empty(), "a symlink is not a Finder alias -- no AliasLink recorded for it");
        assert!(names.contains(&"direct_clip.mov".to_string()));
        assert!(
            names.contains(&"linked_clip.mov".to_string()),
            "a file reachable only through a symlinked subfolder must still be discovered"
        );

        std::fs::remove_dir_all(&dir).ok();
        std::fs::remove_dir_all(&outside).ok();
    }

    /// Creates a real Finder alias (via `osascript`/Finder, the same path
    /// `make_finder_alias`'s caller would use in production) rather than a
    /// symlink -- confirming `is_finder_alias`/`resolve_finder_alias` and
    /// not just walkdir's own `follow_links`. Returns `false` (skip, don't
    /// fail the test) if `osascript` itself is unavailable/blocked in this
    /// environment, since that's an environment gap, not a code regression.
    #[cfg(target_os = "macos")]
    fn make_finder_alias(target: &Path, alias_container: &Path) -> bool {
        use std::io::Write;
        use std::process::{Command, Stdio};

        let script = format!(
            "tell application \"Finder\"\n\
             make new alias file at (POSIX file \"{}\") to (POSIX file \"{}\")\n\
             end tell",
            alias_container.to_string_lossy(),
            target.to_string_lossy(),
        );
        let Ok(mut child) = Command::new("osascript")
            .stdin(Stdio::piped())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
        else {
            return false;
        };
        let Some(mut stdin) = child.stdin.take() else {
            return false;
        };
        if stdin.write_all(script.as_bytes()).is_err() {
            return false;
        }
        drop(stdin);
        child.wait().map(|s| s.success()).unwrap_or(false)
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn discover_media_files_follows_a_real_finder_alias_to_a_folder() {
        // The actual Athletics-folder bug report: a *Finder alias*
        // ("Make Alias"), not a symlink, sitting inside a watched root and
        // pointing out to a folder on a second drive. Unlike the symlink
        // case, `WalkDir::follow_links` does nothing for this -- it's an
        // ordinary regular file containing bookmark data, so this only
        // passes if `is_finder_alias`/`resolve_finder_alias` actually work.
        let dir = scratch_dir("finder_alias_subfolder");
        std::fs::write(dir.join("direct_clip.mov"), b"data").unwrap();
        let outside = scratch_dir("finder_alias_target");
        std::fs::write(outside.join("linked_clip.mov"), b"data").unwrap();

        if !make_finder_alias(&outside, &dir) {
            eprintln!("skipping: osascript/Finder alias creation unavailable in this environment");
            std::fs::remove_dir_all(&dir).ok();
            std::fs::remove_dir_all(&outside).ok();
            return;
        }

        let extensions = default_extensions_owned();
        let (found, alias_links) = discover_media_files(&dir, &extensions);
        let names: Vec<String> = found
            .iter()
            .map(|p| p.file_name().unwrap().to_string_lossy().into_owned())
            .collect();

        assert!(names.contains(&"direct_clip.mov".to_string()));
        assert!(
            names.contains(&"linked_clip.mov".to_string()),
            "a file reachable only through a real Finder alias to a folder must still be discovered"
        );

        // The folder-tree/folder_path-filter half of the Athletics bug:
        // without this, the alias's contents are discovered and indexed
        // but permanently unreachable from either feature, since both
        // derive "what's under this watched root" from clips.file_path
        // string prefixes alone, which the alias's cross-volume target
        // shares none of with `dir`.
        assert_eq!(alias_links.len(), 1);
        assert!(
            alias_links[0].apparent_path.starts_with(&dir),
            "the alias's own path is inside the watched root, not its resolved target"
        );
        assert_eq!(
            std::fs::canonicalize(&alias_links[0].real_path).unwrap(),
            std::fs::canonicalize(&outside).unwrap(),
        );

        std::fs::remove_dir_all(&dir).ok();
        std::fs::remove_dir_all(&outside).ok();
    }

    #[test]
    fn scan_and_register_queues_each_new_clip_for_gap_fill_immediately() {
        // Regression coverage for the "stuck at 20" bug: gap-fill jobs
        // used to only get queued via a single sweep at the very end of
        // `rescan_root`, so files registered early in a long scan (one
        // with a slow file later in the walk) sat unqueued until the
        // *entire* scan finished. `scan_and_register` alone -- with no
        // `rescan_root`/`enqueue_pending_gap_fill_jobs` call at all --
        // must queue each clip as it's registered.
        let dir = scratch_dir("per_file_enqueue");
        std::fs::write(dir.join("clip1.mov"), b"hello world").unwrap();
        std::fs::write(dir.join("clip2.mov"), b"more data").unwrap();

        let db_path = dir.join("test_index.sqlite");
        let db = Db::open_at(&db_path).unwrap();
        let extensions = default_extensions_owned();

        let stats = scan_and_register(&db, &dir, &extensions).unwrap();
        assert_eq!(stats.registered, 2);

        let conn = db.conn.lock().unwrap();
        let pending_count: i64 = conn
            .query_row("SELECT COUNT(*) FROM gap_fill_jobs WHERE status = 'pending'", [], |r| r.get(0))
            .unwrap();
        assert_eq!(pending_count, 2, "both newly registered clips should already be queued");

        drop(conn);
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn is_under_a_removed_root_matches_exact_and_nested_paths_but_not_a_sibling_prefix() {
        let removed = vec!["/Volumes/Archive".to_string()];
        assert!(is_under_a_removed_root("/Volumes/Archive", &removed), "exact match");
        assert!(is_under_a_removed_root("/Volumes/Archive/clip1.mov", &removed), "nested file");
        assert!(is_under_a_removed_root("/Volumes/Archive/Sub/clip1.mov", &removed), "nested subfolder");
        assert!(
            !is_under_a_removed_root("/Volumes/Archive2/clip1.mov", &removed),
            "a sibling that merely shares a text prefix must not be excluded"
        );
    }

    #[test]
    fn scan_and_register_never_reregisters_a_file_under_a_removed_watched_root() {
        // Regression coverage for files "repopulating" after the user
        // explicitly removed a folder: a broader/overlapping root's scan
        // (e.g. a season folder containing a shoot subfolder the user
        // removed on its own) must not silently walk straight back over
        // ground the user already asked to be rid of.
        let dir = scratch_dir("removed_root_exclusion");
        let removed_subdir = dir.join("RemovedShoot");
        std::fs::create_dir_all(&removed_subdir).unwrap();
        std::fs::write(removed_subdir.join("clip1.mov"), b"hello world").unwrap();
        std::fs::write(dir.join("clip2.mov"), b"still active").unwrap();

        let db_path = dir.join("test_index.sqlite");
        let db = Db::open_at(&db_path).unwrap();
        {
            let conn = db.conn.lock().unwrap();
            db::add_watched_root(
                &conn,
                &crate::models::NewWatchedRoot {
                    label: "Removed Shoot".to_string(),
                    path: removed_subdir.to_string_lossy().into_owned(),
                    volume_id: None,
                    approved_by: None,
                },
            )
            .unwrap();
            let removed_id: i64 = conn
                .query_row("SELECT id FROM watched_roots WHERE label = 'Removed Shoot'", [], |r| r.get(0))
                .unwrap();
            db::remove_watched_root(&conn, removed_id).unwrap();
        }

        let extensions = default_extensions_owned();
        // Scanning the *parent* folder covers the removed subfolder too --
        // this is the broader/overlapping root from the bug report.
        let stats = scan_and_register(&db, &dir, &extensions).unwrap();

        assert_eq!(stats.discovered, 2, "both files are still discovered by the walk");
        assert_eq!(stats.registered, 1, "only the file outside the removed root gets registered");
        assert_eq!(stats.excluded_removed, 1);

        let conn = db.conn.lock().unwrap();
        let clips = db::list_clips(&conn).unwrap();
        assert_eq!(clips.len(), 1);
        assert!(clips[0].file_path.ends_with("clip2.mov"));

        drop(conn);
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn scan_and_register_reclaims_a_removed_subfolder_once_a_broader_active_root_covers_it() {
        // The Campus Photoshoot bug, found live: a narrow root gets removed,
        // then a *broader, currently-active* root is added over the same
        // location later (e.g. consolidating several event-specific roots
        // under one season-wide root during an annual drive reorganization).
        // Unlike the sibling test above (no active root ever reclaims the
        // ground), here an active root legitimately covers it, and its
        // files must become visible again rather than staying excluded
        // forever just because a narrower root was once removed.
        let dir = scratch_dir("reclaimed_after_active_root");
        let removed_subdir = dir.join("Campus Photoshoot 2025");
        std::fs::create_dir_all(&removed_subdir).unwrap();
        std::fs::write(removed_subdir.join("clip1.mov"), b"hello world").unwrap();

        let db_path = dir.join("test_index.sqlite");
        let db = Db::open_at(&db_path).unwrap();
        {
            let conn = db.conn.lock().unwrap();
            db::add_watched_root(
                &conn,
                &crate::models::NewWatchedRoot {
                    label: "Campus Photoshoot 2025".to_string(),
                    path: removed_subdir.to_string_lossy().into_owned(),
                    volume_id: None,
                    approved_by: None,
                },
            )
            .unwrap();
            let removed_id: i64 = conn
                .query_row("SELECT id FROM watched_roots WHERE label = 'Campus Photoshoot 2025'", [], |r| r.get(0))
                .unwrap();
            db::remove_watched_root(&conn, removed_id).unwrap();

            // The broader, currently-active root that now also covers the
            // removed subfolder's path.
            db::add_watched_root(
                &conn,
                &crate::models::NewWatchedRoot {
                    label: "Activities and Events".to_string(),
                    path: dir.to_string_lossy().into_owned(),
                    volume_id: None,
                    approved_by: None,
                },
            )
            .unwrap();
        }

        let extensions = default_extensions_owned();
        let stats = scan_and_register(&db, &dir, &extensions).unwrap();

        assert_eq!(
            stats.registered, 1,
            "a broader active root must reclaim a subfolder a narrower removed root previously excluded"
        );
        assert_eq!(stats.excluded_removed, 0);

        let conn = db.conn.lock().unwrap();
        let clips = db::list_clips(&conn).unwrap();
        assert_eq!(clips.len(), 1);
        assert!(clips[0].file_path.ends_with("clip1.mov"));

        drop(conn);
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn scan_and_register_relinks_a_moved_file_by_checksum_instead_of_registering_it_as_new() {
        // The annual working-drive-to-archive-drive migration: the same
        // content reappears at a new path after the old one is gone. Must
        // repoint the existing clip (preserving every shot/embedding/tag
        // built on it) rather than treat it as an unrelated new clip.
        let old_dir = scratch_dir("relink_old");
        let old_path = old_dir.join("clip1.mov");
        std::fs::write(&old_path, b"identical bytes").unwrap();

        let db_path = old_dir.join("test_index.sqlite");
        let db = Db::open_at(&db_path).unwrap();
        let extensions = default_extensions_owned();
        let stats = scan_and_register(&db, &old_dir, &extensions).unwrap();
        assert_eq!(stats.registered, 1);

        let original_clip_id = {
            let conn = db.conn.lock().unwrap();
            db::list_clips(&conn).unwrap()[0].id
        };

        // Simulate the move: delete the old file, write the identical bytes
        // at a new location, scan there instead.
        std::fs::remove_file(&old_path).unwrap();
        let new_dir = scratch_dir("relink_new");
        std::fs::write(new_dir.join("clip1.mov"), b"identical bytes").unwrap();

        let stats = scan_and_register(&db, &new_dir, &extensions).unwrap();
        assert_eq!(stats.relinked, 1, "moved content must be relinked, not registered as a new clip");
        assert_eq!(stats.registered, 0);

        let conn = db.conn.lock().unwrap();
        let clips = db::list_clips(&conn).unwrap();
        assert_eq!(clips.len(), 1, "the move must not create a second clip row");
        assert_eq!(clips[0].id, original_clip_id, "the original clip's id (and everything keyed to it) must be preserved");
        assert!(clips[0].file_path.ends_with("clip1.mov"));
        assert_eq!(Path::new(&clips[0].file_path).parent().unwrap(), new_dir);

        drop(conn);
        std::fs::remove_dir_all(&old_dir).ok();
        std::fs::remove_dir_all(&new_dir).ok();
    }

    #[test]
    fn scan_and_register_does_not_relink_when_the_old_copy_still_exists() {
        // A duplicate copy sitting on both drives simultaneously (not a
        // move) must stay two separate clips -- relinking would silently
        // disconnect the original path from its own clip row while it's
        // still right there on disk.
        let old_dir = scratch_dir("duplicate_old");
        std::fs::write(old_dir.join("clip1.mov"), b"identical bytes").unwrap();

        let db_path = old_dir.join("test_index.sqlite");
        let db = Db::open_at(&db_path).unwrap();
        let extensions = default_extensions_owned();
        scan_and_register(&db, &old_dir, &extensions).unwrap();

        let new_dir = scratch_dir("duplicate_new");
        std::fs::write(new_dir.join("clip1.mov"), b"identical bytes").unwrap();

        let stats = scan_and_register(&db, &new_dir, &extensions).unwrap();
        assert_eq!(stats.relinked, 0);
        assert_eq!(stats.registered, 1, "a genuine duplicate must be registered as its own clip, not relinked");

        let conn = db.conn.lock().unwrap();
        assert_eq!(db::list_clips(&conn).unwrap().len(), 2);

        drop(conn);
        std::fs::remove_dir_all(&old_dir).ok();
        std::fs::remove_dir_all(&new_dir).ok();
    }

    #[test]
    fn scan_and_register_is_idempotent_and_reports_stats() {
        let dir = scratch_dir("register");
        std::fs::write(dir.join("clip1.mov"), b"hello world").unwrap();
        std::fs::write(dir.join("clip2.mov"), b"more data").unwrap();

        let db_path = dir.join("test_index.sqlite");
        let db = Db::open_at(&db_path).unwrap();
        let extensions = default_extensions_owned();

        let first = scan_and_register(&db, &dir, &extensions).unwrap();
        assert_eq!(first.discovered, 2);
        assert_eq!(first.registered, 2);
        assert_eq!(first.already_registered, 0);

        let second = scan_and_register(&db, &dir, &extensions).unwrap();
        assert_eq!(second.discovered, 2);
        assert_eq!(second.registered, 0);
        assert_eq!(second.already_registered, 2);

        let conn = db.conn.lock().unwrap();
        assert_eq!(db::list_clips(&conn).unwrap().len(), 2);

        drop(conn);
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn compute_checksum_is_stable_and_content_sensitive() {
        let dir = scratch_dir("checksum");
        let path_a = dir.join("a.mov");
        let path_b = dir.join("b.mov");
        std::fs::write(&path_a, b"identical content").unwrap();
        std::fs::write(&path_b, b"different content!").unwrap();

        let hash_a1 = compute_checksum(&path_a).unwrap();
        let hash_a2 = compute_checksum(&path_a).unwrap();
        let hash_b = compute_checksum(&path_b).unwrap();

        assert_eq!(hash_a1, hash_a2);
        assert_ne!(hash_a1, hash_b);

        std::fs::remove_dir_all(&dir).ok();
    }

    fn fixture_root(overrides: impl FnOnce(&mut WatchedRoot)) -> WatchedRoot {
        let mut root = WatchedRoot {
            id: 1,
            label: "Test Root".to_string(),
            path: "/tmp/does-not-matter".to_string(),
            volume_id: None,
            access_level: "active".to_string(),
            approved_by: None,
            approved_at: "2026-01-01T00:00:00.000Z".to_string(),
            last_scanned_at: None,
            sidecar_cache_enabled: false,
        };
        overrides(&mut root);
        root
    }

    #[test]
    fn roots_due_for_rescan_includes_never_scanned_active_roots() {
        let roots = vec![fixture_root(|_| {})];
        let now = Utc::now();
        let due = roots_due_for_rescan(&roots, now, Duration::from_secs(3600));
        assert_eq!(due.len(), 1);
    }

    #[test]
    fn roots_due_for_rescan_excludes_recently_scanned_roots() {
        let recent = (Utc::now() - chrono::Duration::minutes(5)).to_rfc3339();
        let roots = vec![fixture_root(|r| r.last_scanned_at = Some(recent))];
        let due = roots_due_for_rescan(&roots, Utc::now(), Duration::from_secs(3600));
        assert!(due.is_empty());
    }

    #[test]
    fn roots_due_for_rescan_includes_stale_roots() {
        let stale = (Utc::now() - chrono::Duration::hours(7)).to_rfc3339();
        let roots = vec![fixture_root(|r| r.last_scanned_at = Some(stale))];
        let due = roots_due_for_rescan(&roots, Utc::now(), Duration::from_secs(3600 * 6));
        assert_eq!(due.len(), 1);
    }

    #[test]
    fn roots_due_for_rescan_excludes_paused_and_removed_roots() {
        let roots = vec![
            fixture_root(|r| r.access_level = "paused".to_string()),
            fixture_root(|r| r.access_level = "removed".to_string()),
        ];
        let due = roots_due_for_rescan(&roots, Utc::now(), Duration::from_secs(1));
        assert!(due.is_empty());
    }

    #[test]
    fn roots_due_for_rescan_treats_unparseable_timestamp_as_due() {
        let roots = vec![fixture_root(|r| r.last_scanned_at = Some("garbage".to_string()))];
        let due = roots_due_for_rescan(&roots, Utc::now(), Duration::from_secs(3600));
        assert_eq!(due.len(), 1);
    }

    #[test]
    fn rescan_root_registers_files_touches_scanned_at_and_enqueues_gap_fill() {
        let dir = scratch_dir("rescan_root");
        std::fs::write(dir.join("clip1.mov"), b"hello world").unwrap();

        let db_path = dir.join("test_index.sqlite");
        let db = Db::open_at(&db_path).unwrap();

        let root = {
            let conn = db.conn.lock().unwrap();
            db::add_watched_root(
                &conn,
                &crate::models::NewWatchedRoot {
                    label: "Test".to_string(),
                    path: dir.to_string_lossy().into_owned(),
                    volume_id: None,
                    approved_by: None,
                },
            )
            .unwrap()
        };
        assert!(root.last_scanned_at.is_none());

        let extensions = default_extensions_owned();
        let stats = rescan_root(&db, &root, &extensions).unwrap();
        assert_eq!(stats.registered, 1);

        let conn = db.conn.lock().unwrap();
        let updated = db::list_watched_roots(&conn)
            .unwrap()
            .into_iter()
            .find(|r| r.id == root.id)
            .unwrap();
        assert!(updated.last_scanned_at.is_some());

        let progress = db::gap_fill_progress_for_root(&conn, &root.path).unwrap();
        assert_eq!(progress.discovered, 1);
        assert_eq!(progress.queued, 1);

        drop(conn);
        std::fs::remove_dir_all(&dir).ok();
    }
}
