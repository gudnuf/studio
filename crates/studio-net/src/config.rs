//! `$STUDIO_HOME/studio.toml`: where this home syncs. Nothing about a deploy is hardcoded;
//! relay and Blossom URLs come from here, overridden by `STUDIO_RELAY` / `STUDIO_BLOSSOM`
//! (comma-separated, for tests and one-off runs).
//!
//! ```toml
//! [sync]
//! relays = ["ws://nous.local:7777"]
//! blossom = ["http://nous.local:3000"]
//! device_name = "nous"          # default: $STUDIO_DEVICE, else the hostname
//! ```
//!
//! Writes go through `toml_edit`, so sections and comments this crate doesn't know survive.

use std::fs;
use std::path::{Path, PathBuf};

use serde::Deserialize;

pub const CONFIG_FILE: &str = "studio.toml";

#[derive(Debug, thiserror::Error)]
pub enum ConfigError {
    #[error("{path}: {source}")]
    Io {
        path: PathBuf,
        #[source]
        source: std::io::Error,
    },
    #[error("{path}: {message}")]
    Parse { path: PathBuf, message: String },
    #[error("{0:?} is not a relay URL (ws:// or wss://)")]
    BadRelay(String),
    #[error("{0:?} is not a Blossom URL (http:// or https://)")]
    BadBlossom(String),
}

#[derive(Debug, Clone, Default, PartialEq, Eq, Deserialize)]
pub struct Config {
    #[serde(default)]
    pub sync: SyncConfig,
}

#[derive(Debug, Clone, Default, PartialEq, Eq, Deserialize)]
pub struct SyncConfig {
    #[serde(default)]
    pub relays: Vec<String>,
    #[serde(default)]
    pub blossom: Vec<String>,
    #[serde(default)]
    pub device_name: Option<String>,
}

pub fn path(home: &Path) -> PathBuf {
    home.join(CONFIG_FILE)
}

pub fn check_relay(url: &str) -> Result<(), ConfigError> {
    if (url.starts_with("ws://") || url.starts_with("wss://"))
        && url.len() > 6
        && !url.contains(char::is_whitespace)
    {
        Ok(())
    } else {
        Err(ConfigError::BadRelay(url.to_string()))
    }
}

pub fn check_blossom(url: &str) -> Result<(), ConfigError> {
    if (url.starts_with("http://") || url.starts_with("https://"))
        && url.len() > 8
        && !url.contains(char::is_whitespace)
    {
        Ok(())
    } else {
        Err(ConfigError::BadBlossom(url.to_string()))
    }
}

fn split_list(v: &str) -> Vec<String> {
    v.split(',')
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .map(String::from)
        .collect()
}

impl Config {
    /// The file as written; a missing file is the default (no relays, no Blossom).
    pub fn load(home: &Path) -> Result<Self, ConfigError> {
        let p = path(home);
        let text = match fs::read_to_string(&p) {
            Ok(t) => t,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(Self::default()),
            Err(source) => return Err(ConfigError::Io { path: p, source }),
        };
        let c: Self = toml::from_str(&text).map_err(|e| ConfigError::Parse {
            path: p.clone(),
            message: e.message().to_string(),
        })?;
        for r in &c.sync.relays {
            check_relay(r)?;
        }
        for b in &c.sync.blossom {
            check_blossom(b)?;
        }
        Ok(c)
    }

    /// The file plus the environment: what a command should actually use.
    pub fn effective(home: &Path) -> Result<Self, ConfigError> {
        let mut c = Self::load(home)?;
        c.apply_env(|k| std::env::var(k).ok())?;
        Ok(c)
    }

    /// `STUDIO_RELAY` / `STUDIO_BLOSSOM` replace the lists; `device_name` defaults to
    /// `STUDIO_DEVICE`, then the hostname (the same rule as `studio.py emit`).
    pub fn apply_env(&mut self, get: impl Fn(&str) -> Option<String>) -> Result<(), ConfigError> {
        if let Some(v) = get("STUDIO_RELAY") {
            let l = split_list(&v);
            l.iter().try_for_each(|r| check_relay(r))?;
            self.sync.relays = l;
        }
        if let Some(v) = get("STUDIO_BLOSSOM") {
            let l = split_list(&v);
            l.iter().try_for_each(|b| check_blossom(b))?;
            self.sync.blossom = l;
        }
        if self.sync.device_name.as_deref().is_none_or(str::is_empty) {
            self.sync.device_name = get("STUDIO_DEVICE")
                .filter(|s| !s.is_empty())
                .or_else(|| gethostname::gethostname().into_string().ok());
        }
        Ok(())
    }

    /// Sets `[sync] relays` and `blossom` in the file, keeping everything else in it.
    pub fn write_sync_servers(
        home: &Path,
        relays: &[String],
        blossom: &[String],
    ) -> Result<(), ConfigError> {
        relays.iter().try_for_each(|r| check_relay(r))?;
        blossom.iter().try_for_each(|b| check_blossom(b))?;
        let p = path(home);
        let text = match fs::read_to_string(&p) {
            Ok(t) => t,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => String::new(),
            Err(source) => return Err(ConfigError::Io { path: p, source }),
        };
        let mut doc: toml_edit::DocumentMut =
            text.parse()
                .map_err(|e: toml_edit::TomlError| ConfigError::Parse {
                    path: p.clone(),
                    message: e.message().to_string(),
                })?;
        let sync = doc
            .entry("sync")
            .or_insert_with(toml_edit::table)
            .as_table_mut()
            .ok_or_else(|| ConfigError::Parse {
                path: p.clone(),
                message: "`sync` is not a table".into(),
            })?;
        sync["relays"] = toml_edit::value(relays.iter().collect::<toml_edit::Array>());
        sync["blossom"] = toml_edit::value(blossom.iter().collect::<toml_edit::Array>());
        fs::create_dir_all(home).map_err(|source| ConfigError::Io {
            path: home.to_path_buf(),
            source,
        })?;
        let tmp = p.with_extension("toml.tmp");
        fs::write(&tmp, doc.to_string()).map_err(|source| ConfigError::Io {
            path: tmp.clone(),
            source,
        })?;
        fs::rename(&tmp, &p).map_err(|source| ConfigError::Io { path: p, source })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn env<'a>(pairs: &'a [(&'a str, &'a str)]) -> impl Fn(&str) -> Option<String> + 'a {
        move |k| {
            pairs
                .iter()
                .find(|(n, _)| *n == k)
                .map(|(_, v)| v.to_string())
        }
    }

    #[test]
    fn missing_file_is_the_default() {
        let home = tempfile::tempdir().unwrap();
        assert_eq!(Config::load(home.path()).unwrap(), Config::default());
    }

    #[test]
    fn parses_the_sync_section() {
        let home = tempfile::tempdir().unwrap();
        fs::write(
            path(home.path()),
            "[sync]\nrelays = [\"ws://nous.local:7777\"]\nblossom = [\"http://nous.local:3000\"]\ndevice_name = \"mac\"\n",
        )
        .unwrap();
        let c = Config::load(home.path()).unwrap();
        assert_eq!(c.sync.relays, ["ws://nous.local:7777"]);
        assert_eq!(c.sync.blossom, ["http://nous.local:3000"]);
        assert_eq!(c.sync.device_name.as_deref(), Some("mac"));
    }

    #[test]
    fn rejects_bad_urls_and_bad_toml() {
        let home = tempfile::tempdir().unwrap();
        fs::write(
            path(home.path()),
            "[sync]\nrelays = [\"http://not-a-relay\"]\n",
        )
        .unwrap();
        assert!(matches!(
            Config::load(home.path()),
            Err(ConfigError::BadRelay(_))
        ));
        fs::write(path(home.path()), "[sync\n").unwrap();
        assert!(matches!(
            Config::load(home.path()),
            Err(ConfigError::Parse { .. })
        ));
    }

    #[test]
    fn env_overrides_lists_and_names_the_device() {
        let mut c = Config::default();
        c.sync.relays = vec!["wss://file.example".into()];
        c.apply_env(env(&[
            ("STUDIO_RELAY", "ws://a:1, ws://b:2"),
            ("STUDIO_BLOSSOM", "http://c:3"),
            ("STUDIO_DEVICE", "phone"),
        ]))
        .unwrap();
        assert_eq!(c.sync.relays, ["ws://a:1", "ws://b:2"]);
        assert_eq!(c.sync.blossom, ["http://c:3"]);
        assert_eq!(c.sync.device_name.as_deref(), Some("phone"));

        let mut c = Config::default();
        c.apply_env(env(&[])).unwrap();
        assert!(
            c.sync.device_name.is_some_and(|n| !n.is_empty()),
            "hostname fallback"
        );

        let mut c = Config::default();
        assert!(c
            .apply_env(env(&[("STUDIO_BLOSSOM", "ws://nope")]))
            .is_err());
    }

    #[test]
    fn write_keeps_what_it_does_not_own() {
        let home = tempfile::tempdir().unwrap();
        fs::write(
            path(home.path()),
            "# my library\n[render]\nfps = 30 # keep\n\n[sync]\ndevice_name = \"mac\"\nrelays = [\"ws://old:1\"]\n",
        )
        .unwrap();
        Config::write_sync_servers(
            home.path(),
            &["wss://r.example".into()],
            &["https://b.example".into()],
        )
        .unwrap();
        let text = fs::read_to_string(path(home.path())).unwrap();
        assert!(text.contains("# my library") && text.contains("fps = 30 # keep"));
        let c = Config::load(home.path()).unwrap();
        assert_eq!(c.sync.relays, ["wss://r.example"]);
        assert_eq!(c.sync.blossom, ["https://b.example"]);
        assert_eq!(c.sync.device_name.as_deref(), Some("mac"));
    }

    #[test]
    fn write_creates_the_file() {
        let home = tempfile::tempdir().unwrap();
        Config::write_sync_servers(home.path(), &["ws://a:1".into()], &[]).unwrap();
        assert_eq!(Config::load(home.path()).unwrap().sync.relays, ["ws://a:1"]);
    }
}
