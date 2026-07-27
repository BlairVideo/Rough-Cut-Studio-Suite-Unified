-- Needed for accurate Premiere Pro XMEML export (Section 14): every
-- clipitem's in/out/duration must be computed against a frame rate, and
-- the plan explicitly calls out that mixed frame rates across archive
-- footage need to be handled per-clip rather than assumed constant.
-- Probed by the sidecar during gap-fill (cv2.CAP_PROP_FPS), alongside
-- duration_sec which already gets set there.
ALTER TABLE clips ADD COLUMN frame_rate REAL;
