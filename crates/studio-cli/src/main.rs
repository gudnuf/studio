//! `studio-rs`: the Rust studio CLI. Named `studio-rs` until it takes over `studio` (task 2.9);
//! for now it has one verb, `pair`.
//!
//! Secrets: no command prints a key or the pairing payload except `pair --payload`, which
//! exists to print it. Status lines go to stderr and name the library by id and npub only.

use std::io::Read;
use std::path::PathBuf;
use std::process::ExitCode;

use clap::{Parser, Subcommand};
use studio_net::config::Config;
use studio_net::keys::LibraryKeys;
use studio_net::pair::Pairing;

#[derive(Parser)]
#[command(name = "studio-rs", about = "studio, the Rust core (v2, in progress)")]
struct Cli {
    #[command(subcommand)]
    cmd: Cmd,
}

#[derive(Subcommand)]
enum Cmd {
    /// Pair a device with this library: show the pairing QR, or join from a payload.
    ///
    /// The payload holds the library's secret keys (v1: one shared key). Anyone who scans the
    /// QR or reads the payload can read and write the whole library.
    Pair {
        /// Print the pairing payload as a terminal QR code (makes the library key if missing).
        #[arg(long, group = "mode")]
        qr: bool,
        /// Print the pairing payload as text, for pasting into `pair --from` on another home.
        #[arg(long, group = "mode")]
        payload: bool,
        /// Join the library in PAYLOAD (`-` reads it from stdin, keeping it out of `ps` and
        /// shell history). Writes keys/ and the [sync] servers in studio.toml.
        #[arg(long, value_name = "PAYLOAD", group = "mode")]
        from: Option<String>,
    },
}

fn home() -> PathBuf {
    match std::env::var_os("STUDIO_HOME") {
        Some(h) if !h.is_empty() => PathBuf::from(h),
        _ => dirs_home().join("Studio"),
    }
}

fn dirs_home() -> PathBuf {
    std::env::var_os("HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("."))
}

fn main() -> ExitCode {
    let cli = Cli::parse();
    match run(cli) {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("studio-rs: {e}");
            ExitCode::FAILURE
        }
    }
}

type Res = Result<(), Box<dyn std::error::Error>>;

fn run(cli: Cli) -> Res {
    match cli.cmd {
        Cmd::Pair { qr, payload, from } => match (qr, payload, from) {
            (_, _, Some(src)) => pair_from(&src),
            (false, false, None) => Err("pair: pass --qr, --payload or --from PAYLOAD".into()),
            (_, show_text, None) => pair_show(show_text),
        },
    }
}

fn pair_show(as_text: bool) -> Res {
    let home = home();
    std::fs::create_dir_all(&home)?;
    let (keys, created) = LibraryKeys::load_or_create(&home)?;
    if created {
        eprintln!(
            "made the library key: {}",
            LibraryKeys::dir(&home).display()
        );
    }
    let cfg = Config::effective(&home)?;
    let p = Pairing {
        keys,
        relays: cfg.sync.relays,
        blossom: cfg.sync.blossom,
    };
    if as_text {
        println!("{}", p.encode());
    } else {
        print!("{}", p.qr());
        println!();
    }
    eprintln!("library {}  {}", p.keys.library_id(), p.keys.npub());
    eprintln!("  relays:  {}", list(&p.relays));
    eprintln!("  blossom: {}", list(&p.blossom));
    if p.relays.is_empty() || p.blossom.is_empty() {
        eprintln!(
            "  note: set [sync] relays and blossom in {} first",
            studio_net::config::path(&home).display()
        );
    }
    eprintln!(
        "this holds the library's secret keys: anyone who has it can read and write the library"
    );
    Ok(())
}

fn pair_from(src: &str) -> Res {
    let text = if src == "-" {
        let mut s = String::new();
        std::io::stdin().read_to_string(&mut s)?;
        s
    } else {
        src.to_string()
    };
    let p = Pairing::decode(&text)?;
    let home = home();
    std::fs::create_dir_all(&home)?;
    match LibraryKeys::load(&home)? {
        Some(existing) if existing == p.keys => {
            eprintln!("already paired with library {}", p.keys.library_id());
        }
        Some(existing) => {
            return Err(format!(
                "{} already belongs to library {}; refusing to replace its keys with library {}",
                home.display(),
                existing.library_id(),
                p.keys.library_id()
            )
            .into());
        }
        None => {
            p.keys.save(&home)?;
            eprintln!(
                "paired with library {}  {}",
                p.keys.library_id(),
                p.keys.npub()
            );
        }
    }
    // An empty list in the payload means "not set there", not "forget mine".
    if !p.relays.is_empty() || !p.blossom.is_empty() {
        let cur = Config::load(&home)?.sync;
        let relays = if p.relays.is_empty() {
            cur.relays
        } else {
            p.relays
        };
        let blossom = if p.blossom.is_empty() {
            cur.blossom
        } else {
            p.blossom
        };
        Config::write_sync_servers(&home, &relays, &blossom)?;
    }
    let cfg = Config::load(&home)?.sync;
    eprintln!("  relays:  {}", list(&cfg.relays));
    eprintln!("  blossom: {}", list(&cfg.blossom));
    Ok(())
}

fn list(v: &[String]) -> String {
    if v.is_empty() {
        "(none)".into()
    } else {
        v.join(", ")
    }
}
