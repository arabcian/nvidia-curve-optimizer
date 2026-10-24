//! nvcurve-boot-guard — keeps an unstable boot-time curve from crash-looping the machine.
//!
//!   nvcurve-boot-guard arm        (root) start of boot: arm, or trip if the last boot died armed
//!   nvcurve-boot-guard watch      (root) arm, disarm after the stable window, stay up; SIGTERM =
//!                                  clean shutdown (systemd Type=simple; OpenRC uses arm + stop)
//!   nvcurve-boot-guard disarm     (root) this boot is fine (stable window over)
//!   nvcurve-boot-guard shutdown   (root) clean shutdown/reboot: disarm + record it (OpenRC stop)
//!   nvcurve-boot-guard check      exit 0 = apply the profile, 1 = skip (reason on stderr) — for
//!                                  systemd ExecCondition= and the OpenRC script
//!   nvcurve-boot-guard status     print the state as JSON
//!   nvcurve-boot-guard reset      (root) resume the autoload after a trip
//!
//! See bootguard.rs for the state machine.

use nvcurve::bootguard;
use std::sync::atomic::{AtomicBool, Ordering};

static STOP: AtomicBool = AtomicBool::new(false);
extern "C" fn on_term(_: libc::c_int) { STOP.store(true, Ordering::SeqCst); }

fn fail(e: String) -> i32 { eprintln!("nvcurve-boot-guard: {e}"); 2 }

fn arm() -> i32 {
    match bootguard::arm() {
        Ok(v) => {
            if v["tripped"] == true {
                eprintln!("nvcurve-boot-guard: boot-time curve PAUSED — {}. Resume it with: sudo nvcurve-boot-guard reset",
                          v["reason"].as_str().unwrap_or("previous boot failed"));
            }
            0
        }
        Err(e) => fail(e),
    }
}

fn watch() -> i32 {
    let code = arm();
    if code != 0 || bootguard::read()["state"] != "armed" { return code; }
    for s in [libc::SIGTERM, libc::SIGINT, libc::SIGHUP] {
        unsafe { libc::signal(s, on_term as *const () as libc::sighandler_t) };
    }
    // Sleep in short steps so a shutdown within the window disarms promptly.
    let end = std::time::Instant::now() + std::time::Duration::from_secs(bootguard::WINDOW_SECS);
    while std::time::Instant::now() < end && !STOP.load(Ordering::SeqCst) {
        std::thread::sleep(std::time::Duration::from_millis(250));
    }
    if !STOP.load(Ordering::SeqCst) {
        if let Err(e) = bootguard::disarm() { fail(e); }
        // Stay up until the service is stopped: SIGTERM at shutdown is how a clean end of this
        // boot gets recorded. Blocked in sigsuspend() until then; the signals are blocked outside
        // sigsuspend, so one cannot slip in between the check and the wait.
        unsafe {
            let (mut block, mut old): (libc::sigset_t, libc::sigset_t) = (std::mem::zeroed(), std::mem::zeroed());
            libc::sigemptyset(&mut block);
            for s in [libc::SIGTERM, libc::SIGINT, libc::SIGHUP] { libc::sigaddset(&mut block, s); }
            libc::sigprocmask(libc::SIG_BLOCK, &block, &mut old);
            while !STOP.load(Ordering::SeqCst) { libc::sigsuspend(&old); }
            libc::sigprocmask(libc::SIG_SETMASK, &old, std::ptr::null_mut());
        }
    }
    bootguard::shutdown().map_or_else(fail, |_| 0)
}

fn main() {
    // No core dumps: a crash must not leave guard state or root memory in a file.
    unsafe {
        libc::umask(0o022);
        let zero = libc::rlimit { rlim_cur: 0, rlim_max: 0 };
        libc::setrlimit(libc::RLIMIT_CORE, &zero);
    }
    let code = match std::env::args().nth(1).as_deref() {
        Some("arm") => arm(),
        Some("watch") => watch(),
        Some("disarm") => bootguard::disarm().map_or_else(fail, |_| 0),
        Some("shutdown") => bootguard::shutdown().map_or_else(fail, |_| 0),
        Some("check") => match bootguard::should_skip() {
            None => 0,
            Some(why) => { eprintln!("nvcurve-boot-guard: skipping boot-time curve — {why}"); 1 }
        },
        Some("status") => { println!("{}", bootguard::read()); 0 }
        Some("reset") => bootguard::reset().map_or_else(fail, |v| { println!("{v}"); 0 }),
        _ => { eprintln!("usage: nvcurve-boot-guard arm|watch|disarm|shutdown|check|status|reset"); 2 }
    };
    std::process::exit(code);
}
