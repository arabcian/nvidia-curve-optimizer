//! Boot guard: pauses the boot-time curve profile after a boot that ended badly.
//!
//! An undervolt / curve / tuning preset that is too aggressive can crash the
//! machine every time it is applied at boot — a crash loop the user can only
//! escape from a rescue shell. The guard gives every boot a short "armed"
//! window: `arm` at boot, `disarm` after WINDOW of stable uptime or on a clean
//! shutdown. A boot that never disarmed (panic, hang, forced power-off while
//! the presets were fresh) trips the guard, and from then on the autoload
//! service skips until the user resumes it with `nvcurve-boot-guard reset` — they are not
//! re-enabled automatically, so there is no crash / skip / crash cycle.
//!
//! State (root-owned, world-readable so the GUI can show it):
//!   /var/lib/nvcurve/boot-guard.json
//!   {"boot_id", "state": "armed"|"ok"|"paused", "tripped": bool, "reason", "tripped_at",
//!    "clean_shutdown": boot id of the last boot that shut down cleanly}

use serde_json::{json, Value};
use std::io::Read;
use std::os::unix::fs::{MetadataExt, OpenOptionsExt};
use std::path::Path;
use std::time::{SystemTime, UNIX_EPOCH};

pub const GUARD_DIR: &str = "/var/lib/nvcurve";
pub const GUARD_FILE: &str = "/var/lib/nvcurve/boot-guard.json";
/// Stable uptime after which the presets are considered good for this boot.
pub const WINDOW_SECS: u64 = 180;

pub fn boot_id() -> String {
    std::fs::read_to_string("/proc/sys/kernel/random/boot_id").map(|s| s.trim().to_owned()).unwrap_or_default()
}

/// Reads a root-owned, not group/other-writable regular file (no symlinks), at most `max` bytes.
/// The checks run on the opened descriptor, so the file cannot be swapped between check and read.
fn read_root_file(p: &str, max: u64) -> Option<String> {
    let f = std::fs::OpenOptions::new().read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC | libc::O_NONBLOCK).open(p).ok()?;
    let m = f.metadata().ok()?;
    if !m.is_file() || m.uid() != 0 || m.mode() & 0o022 != 0 || m.len() > max { return None; }
    let mut s = String::new();
    (&f).take(max + 1).read_to_string(&mut s).ok()?;
    (s.len() as u64 <= max).then_some(s)
}

pub fn read() -> Value {
    read_root_file(GUARD_FILE, 16 * 1024)
        .and_then(|s| serde_json::from_str::<Value>(&s).ok())
        .filter(Value::is_object)
        .unwrap_or_else(|| json!({}))
}

fn write(v: &Value) -> Result<(), String> {
    crate::atomicio::ensure_dir(Path::new(GUARD_DIR), 0o755).map_err(|e| format!("{GUARD_DIR}: {e}"))?;
    crate::atomicio::write_json(Path::new(GUARD_FILE), v, 0o644).map_err(|e| format!("{GUARD_FILE}: {e}"))
}

/// A previous boot left the guard armed (it never reached its stable window).
fn previous_boot_died(v: &Value, current: &str) -> bool {
    v["state"] == "armed" && v["boot_id"].as_str().map_or(false, |b| b != current)
}

/// Presets must be skipped: already tripped, or the previous boot died armed
/// (evaluated here too, so a preset service that starts before `arm` has run
/// still decides correctly).
pub fn should_skip() -> Option<String> {
    let v = read();
    if v["tripped"] == true {
        return Some(v["reason"].as_str().unwrap_or("boot presets are paused").to_owned());
    }
    if previous_boot_died(&v, &boot_id()) {
        return Some("the previous boot did not stay up after the presets were applied".into());
    }
    None
}

fn now() -> u64 { SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0) }

/// Start of a boot: trips the guard if the previous boot died armed, else arms
/// it for this boot. Idempotent within one boot.
pub fn arm() -> Result<Value, String> {
    let mut v = read();
    let cur = boot_id();
    if cur.is_empty() { return Err("no boot_id".into()); }
    if v["boot_id"] == json!(cur) { return Ok(v); }
    if previous_boot_died(&v, &cur) {
        v["tripped"] = json!(true);
        v["tripped_at"] = json!(now());
        v["reason"] = json!(format!(
            "the previous boot ended within {} minutes of applying the boot-time curve without a clean shutdown \
             (crash, hang or forced power-off) — an unstable undervolt or curve is the usual cause",
            WINDOW_SECS / 60));
    }
    v["boot_id"] = json!(cur);
    v["state"] = json!(if v["tripped"] == true { "paused" } else { "armed" });
    write(&v)?;
    Ok(v)
}

/// This boot survived its window (or is shutting down cleanly).
pub fn disarm() -> Result<(), String> {
    let mut v = read();
    if v["boot_id"] == json!(boot_id()) && v["state"] == "armed" {
        v["state"] = json!("ok");
        write(&v)?;
    }
    Ok(())
}

/// Clean shutdown / reboot (OpenRC `stop`, systemd SIGTERM to `watch`):
/// disarms if still armed and records this boot id as having ended cleanly.
pub fn shutdown() -> Result<(), String> {
    let mut v = read();
    let cur = boot_id();
    if cur.is_empty() { return Err("no boot_id".into()); }
    if v["boot_id"] == json!(cur) && v["state"] == "armed" { v["state"] = json!("ok"); }
    if v["boot_id"].is_null() { v["boot_id"] = json!(cur); }
    v["clean_shutdown"] = json!(cur);
    write(&v)
}

/// User resumed the presets (`nvcurve-boot-guard reset`).
/// They apply again from the next boot on (or right away via each tab).
pub fn reset() -> Result<Value, String> {
    let mut v = read();
    v["tripped"] = json!(false);
    v["reason"] = Value::Null;
    v["tripped_at"] = Value::Null;
    if v["state"] == "paused" { v["state"] = json!("ok"); }
    if v["boot_id"].is_null() { v["boot_id"] = json!(boot_id()); }
    write(&v)?;
    Ok(v)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn died_detection() {
        assert!(previous_boot_died(&json!({"state": "armed", "boot_id": "a"}), "b"));
        assert!(!previous_boot_died(&json!({"state": "armed", "boot_id": "b"}), "b"));  // same boot, still in window
        assert!(!previous_boot_died(&json!({"state": "ok", "boot_id": "a"}), "b"));
        assert!(!previous_boot_died(&json!({}), "b"));
    }
}
