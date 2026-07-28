-- The folder tree (folders.rs) and the folder_path search filter
-- (facets.rs) both derive "what's under this watched root" by
-- string-prefix matching clips.file_path against a watched root's own
-- path. A Finder alias's *resolved* target can live on a completely
-- different volume with no path relationship to where the alias itself
-- sits -- so without recording that redirection, an aliased subtree is
-- permanently invisible to both features no matter how many times the
-- root is rescanned (confirmed live: an "Athletics" alias resolving
-- across drives left ~1,500 already-indexed clips unreachable from the
-- folder tree). apparent_path is the alias's own path (as encountered
-- under a watched root, or under another alias's own resolved target);
-- real_path is where it actually resolves. One row per boundary crossing
-- -- nested/chained aliases are supported by recording each hop, not just
-- the top-level one -- see folders::resolve_real_prefix.
CREATE TABLE alias_links (
    apparent_path TEXT PRIMARY KEY,
    real_path     TEXT NOT NULL,
    updated_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
