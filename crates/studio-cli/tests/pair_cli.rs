//! `studio-rs pair` end to end on temp homes, plus the grep test: the library's secrets appear
//! nowhere but `keys/` and the output of `pair --payload`.

use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::{Command, Output, Stdio};

fn bin() -> &'static str {
    env!("CARGO_BIN_EXE_studio-rs")
}

fn run(home: &Path, args: &[&str], stdin: Option<&str>) -> Output {
    let mut c = Command::new(bin());
    c.args(args)
        .env("STUDIO_HOME", home)
        .env_remove("STUDIO_RELAY")
        .env_remove("STUDIO_BLOSSOM")
        .env("STUDIO_DEVICE", "test-device")
        .stdin(if stdin.is_some() {
            Stdio::piped()
        } else {
            Stdio::null()
        })
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    let mut child = c.spawn().unwrap();
    if let Some(s) = stdin {
        child.stdin.take().unwrap().write_all(s.as_bytes()).unwrap();
    }
    child.wait_with_output().unwrap()
}

fn ok(o: &Output) -> (String, String) {
    let (out, err) = (
        String::from_utf8_lossy(&o.stdout).into_owned(),
        String::from_utf8_lossy(&o.stderr).into_owned(),
    );
    assert!(
        o.status.success(),
        "exit {:?}\nstdout:\n{out}\nstderr:\n{err}",
        o.status
    );
    (out, err)
}

/// Every secret spelling of the home's key, read straight from its files.
fn secrets(home: &Path) -> Vec<String> {
    let dir = home.join("keys");
    let nsec = fs::read_to_string(dir.join("nostr.key"))
        .unwrap()
        .trim()
        .to_string();
    let age = fs::read_to_string(dir.join("age.key"))
        .unwrap()
        .lines()
        .find(|l| l.starts_with("AGE-SECRET-KEY-1"))
        .unwrap()
        .to_string();
    let keys = nostr::key::Keys::parse(&nsec).unwrap();
    let hex = keys.secret_key().to_secret_hex();
    assert!(nsec.starts_with("nsec1"));
    vec![
        nsec.clone(),
        age.clone(),
        hex.clone(),
        age.to_lowercase(),
        nsec[5..].to_string(),
    ]
}

fn files_outside_keys(home: &Path) -> Vec<PathBuf> {
    fn walk(d: &Path, out: &mut Vec<PathBuf>) {
        for e in fs::read_dir(d).unwrap() {
            let p = e.unwrap().path();
            if p.is_dir() {
                if p.file_name().unwrap() != "keys" {
                    walk(&p, out);
                }
            } else {
                out.push(p);
            }
        }
    }
    let mut v = vec![];
    walk(home, &mut v);
    v
}

fn assert_clean(what: &str, text: &str, secrets: &[String]) {
    for s in secrets {
        assert!(!text.contains(s.as_str()), "{what} contains a secret");
    }
}

#[test]
fn pair_round_trip_between_two_homes_and_secrets_stay_in_keys() {
    let a = tempfile::tempdir().unwrap();
    let b = tempfile::tempdir().unwrap();
    fs::write(
        a.path().join("studio.toml"),
        "[sync]\nrelays = [\"ws://nous.local:7777\"]\nblossom = [\"http://nous.local:3000\"]\n",
    )
    .unwrap();
    // something that looks like a library log, to check it stays untouched and clean
    fs::write(a.path().join("events.jsonl"), "{\"type\":\"note\"}\n").unwrap();

    let mut transcript = vec![];

    // first run makes the key
    let (qr, err) = ok(&run(a.path(), &["pair", "--qr"], None));
    assert!(err.contains("made the library key"), "{err}");
    assert!(qr.lines().count() > 20, "a QR on stdout");
    transcript.push(("pair --qr stdout", qr));
    transcript.push(("pair --qr stderr", err));
    let sec = secrets(a.path());

    // second run reuses it
    let (_, err) = ok(&run(a.path(), &["pair", "--qr"], None));
    assert!(!err.contains("made the library key"));
    transcript.push(("pair --qr stderr (2)", err));

    let (payload, err) = ok(&run(a.path(), &["pair", "--payload"], None));
    assert!(payload.starts_with("studio-pair:"));
    transcript.push(("pair --payload stderr", err));

    // join from stdin on an empty home
    let (out, err) = ok(&run(b.path(), &["pair", "--from", "-"], Some(&payload)));
    assert!(err.contains("paired with library"), "{err}");
    transcript.push(("pair --from stdout", out));
    transcript.push(("pair --from stderr", err));
    assert_eq!(
        fs::read(b.path().join("keys/nostr.key")).unwrap(),
        fs::read(a.path().join("keys/nostr.key")).unwrap()
    );
    assert_eq!(secrets(b.path()), sec);
    let cfg_b = fs::read_to_string(b.path().join("studio.toml")).unwrap();
    assert!(cfg_b.contains("ws://nous.local:7777") && cfg_b.contains("http://nous.local:3000"));

    // again: idempotent
    let (_, err) = ok(&run(b.path(), &["pair", "--from", payload.trim()], None));
    assert!(err.contains("already paired"), "{err}");
    transcript.push(("pair --from (again) stderr", err));

    // a home that already has another library refuses, and keeps its key
    let c = tempfile::tempdir().unwrap();
    ok(&run(c.path(), &["pair", "--qr"], None));
    let before = fs::read(c.path().join("keys/nostr.key")).unwrap();
    let o = run(c.path(), &["pair", "--from", "-"], Some(&payload));
    assert!(!o.status.success());
    let err = String::from_utf8_lossy(&o.stderr).into_owned();
    assert!(err.contains("refusing to replace"), "{err}");
    assert_eq!(fs::read(c.path().join("keys/nostr.key")).unwrap(), before);
    transcript.push(("refused --from stderr", err));

    // a damaged payload says so without echoing it
    let o = run(
        b.path(),
        &[
            "pair",
            "--from",
            &payload.trim().replace("studio-pair:", "studio-pair:!"),
        ],
        None,
    );
    assert!(!o.status.success());
    transcript.push((
        "damaged --from stderr",
        String::from_utf8_lossy(&o.stderr).into_owned(),
    ));

    // the grep test
    for (what, text) in &transcript {
        assert_clean(what, text, &sec);
        assert!(
            !text.contains(payload.trim()),
            "{what} contains the payload"
        );
    }
    for home in [a.path(), b.path()] {
        for f in files_outside_keys(home) {
            let text = String::from_utf8_lossy(&fs::read(&f).unwrap()).into_owned();
            assert_clean(&f.display().to_string(), &text, &sec);
        }
    }
    assert_eq!(
        fs::read_to_string(a.path().join("events.jsonl")).unwrap(),
        "{\"type\":\"note\"}\n"
    );

    // modes
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let mode = |p: PathBuf| fs::metadata(p).unwrap().permissions().mode() & 0o777;
        for home in [a.path(), b.path()] {
            assert_eq!(mode(home.join("keys")), 0o700);
            assert_eq!(mode(home.join("keys/nostr.key")), 0o600);
            assert_eq!(mode(home.join("keys/age.key")), 0o600);
        }
    }
}

#[test]
fn pair_needs_a_mode() {
    let h = tempfile::tempdir().unwrap();
    let o = run(h.path(), &["pair"], None);
    assert!(!o.status.success());
    assert!(
        !h.path().join("keys").exists(),
        "no key made without a mode"
    );
}
