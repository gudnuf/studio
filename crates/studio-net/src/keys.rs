//! The library key: one nostr keypair (identity, relay auth, NIP-44 self-encryption) and one
//! age x25519 identity (blob encryption), shared by every device of the library in v1.
//!
//! Stored in `$STUDIO_HOME/keys/` (directory 0700, files 0600):
//!   `nostr.key`  the nsec (bech32), one line
//!   `age.key`    age-keygen format: `# public key: age1…` comment + `AGE-SECRET-KEY-1…`
//!
//! `library_id` is the first 16 hex characters of the library's x-only public key.
//! Nothing in this module prints or logs a secret; `Debug` is redacted.

use std::fmt;
use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::str::FromStr;

use age::secrecy::ExposeSecret;
use nostr::nips::nip19::ToBech32;

pub const KEYS_DIR: &str = "keys";
pub const NOSTR_FILE: &str = "nostr.key";
pub const AGE_FILE: &str = "age.key";

#[derive(Debug, thiserror::Error)]
pub enum KeyError {
    #[error("{path}: {source}")]
    Io {
        path: PathBuf,
        #[source]
        source: std::io::Error,
    },
    #[error("{0}: not a valid nostr secret key")]
    BadNostr(PathBuf),
    #[error("{0}: not a valid age identity")]
    BadAge(PathBuf),
    #[error("{present} exists but {missing} does not; refusing to make a new key (that would split the library)")]
    Partial { present: PathBuf, missing: PathBuf },
    #[error("{path} is readable by others (mode {mode:o}); run: chmod 600 {path}")]
    Permissions { path: PathBuf, mode: u32 },
    #[error("{0} already holds a library key; refusing to overwrite it")]
    Exists(PathBuf),
}

fn io(path: &Path) -> impl FnOnce(std::io::Error) -> KeyError + '_ {
    move |source| KeyError::Io {
        path: path.to_path_buf(),
        source,
    }
}

/// The library's two secrets. Cloning copies the secrets; `Debug` never shows them.
#[derive(Clone)]
pub struct LibraryKeys {
    nostr: nostr::key::Keys,
    age: age::x25519::Identity,
}

impl fmt::Debug for LibraryKeys {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("LibraryKeys")
            .field("library_id", &self.library_id())
            .field("npub", &self.npub())
            .field("age_recipient", &self.age_recipient())
            .finish_non_exhaustive()
    }
}

impl PartialEq for LibraryKeys {
    fn eq(&self, other: &Self) -> bool {
        self.nostr.secret_key() == other.nostr.secret_key()
            && self.age.to_string().expose_secret() == other.age.to_string().expose_secret()
    }
}
impl Eq for LibraryKeys {}

impl LibraryKeys {
    /// A fresh library key from the OS RNG.
    pub fn generate() -> Self {
        Self {
            nostr: nostr::key::Keys::generate(),
            age: age::x25519::Identity::generate(),
        }
    }

    /// From the two secret strings (`nsec1…` or 64 hex; `AGE-SECRET-KEY-1…`).
    pub fn from_secrets(nostr_secret: &str, age_secret: &str) -> Option<Self> {
        let nostr = nostr::key::Keys::parse(nostr_secret.trim()).ok()?;
        let age = age::x25519::Identity::from_str(age_secret.trim()).ok()?;
        Some(Self { nostr, age })
    }

    pub fn nostr(&self) -> &nostr::key::Keys {
        &self.nostr
    }

    pub fn age_identity(&self) -> &age::x25519::Identity {
        &self.age
    }

    pub fn pubkey_hex(&self) -> String {
        self.nostr.public_key().to_hex()
    }

    pub fn npub(&self) -> String {
        self.nostr
            .public_key()
            .to_bech32()
            .expect("bech32 of a valid public key")
    }

    /// First 16 hex characters of the x-only public key: the library's name on relays (`d`/`L`
    /// tags) and in pairing.
    pub fn library_id(&self) -> String {
        self.pubkey_hex()[..16].to_string()
    }

    pub fn age_recipient(&self) -> String {
        self.age.to_public().to_string()
    }

    /// The nostr secret as `nsec1…`. Only for the pairing payload and the key file.
    pub(crate) fn nsec(&self) -> String {
        self.nostr
            .secret_key()
            .to_bech32()
            .expect("bech32 of a valid secret key")
    }

    /// The age secret as `AGE-SECRET-KEY-1…`. Only for the pairing payload and the key file.
    pub(crate) fn age_secret(&self) -> String {
        self.age.to_string().expose_secret().to_string()
    }

    pub fn dir(home: &Path) -> PathBuf {
        home.join(KEYS_DIR)
    }

    /// The home's key, or `None` when it has none yet. One file without the other is an error,
    /// never a reason to generate.
    pub fn load(home: &Path) -> Result<Option<Self>, KeyError> {
        let dir = Self::dir(home);
        let (np, ap) = (dir.join(NOSTR_FILE), dir.join(AGE_FILE));
        match (np.exists(), ap.exists()) {
            (false, false) => return Ok(None),
            (true, false) => {
                return Err(KeyError::Partial {
                    present: np,
                    missing: ap,
                })
            }
            (false, true) => {
                return Err(KeyError::Partial {
                    present: ap,
                    missing: np,
                })
            }
            (true, true) => {}
        }
        let nostr_text = read_private(&np)?;
        let age_text = read_private(&ap)?;
        let nostr =
            nostr::key::Keys::parse(nostr_text.trim()).map_err(|_| KeyError::BadNostr(np))?;
        let age_line = age_text
            .lines()
            .map(str::trim)
            .find(|l| !l.is_empty() && !l.starts_with('#'))
            .ok_or_else(|| KeyError::BadAge(ap.clone()))?;
        let age = age::x25519::Identity::from_str(age_line).map_err(|_| KeyError::BadAge(ap))?;
        Ok(Some(Self { nostr, age }))
    }

    /// The home's key, generating and saving one on first run. `true` when it was just made.
    pub fn load_or_create(home: &Path) -> Result<(Self, bool), KeyError> {
        if let Some(k) = Self::load(home)? {
            return Ok((k, false));
        }
        let k = Self::generate();
        k.save(home)?;
        Ok((k, true))
    }

    /// Writes both key files (0600) in a 0700 `keys/`. Refuses if either file exists.
    pub fn save(&self, home: &Path) -> Result<(), KeyError> {
        let dir = Self::dir(home);
        let (np, ap) = (dir.join(NOSTR_FILE), dir.join(AGE_FILE));
        if np.exists() || ap.exists() {
            return Err(KeyError::Exists(dir));
        }
        fs::create_dir_all(&dir).map_err(io(&dir))?;
        set_mode(&dir, 0o700)?;
        let age_file = format!(
            "# studio library {} age identity\n# public key: {}\n{}\n",
            self.library_id(),
            self.age_recipient(),
            self.age_secret()
        );
        write_private(&ap, age_file.as_bytes())?;
        write_private(&np, format!("{}\n", self.nsec()).as_bytes())?;
        Ok(())
    }
}

#[cfg(unix)]
fn set_mode(path: &Path, mode: u32) -> Result<(), KeyError> {
    use std::os::unix::fs::PermissionsExt;
    fs::set_permissions(path, fs::Permissions::from_mode(mode)).map_err(io(path))
}
#[cfg(not(unix))]
fn set_mode(_: &Path, _: u32) -> Result<(), KeyError> {
    Ok(())
}

fn read_private(path: &Path) -> Result<String, KeyError> {
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let mode = fs::metadata(path).map_err(io(path))?.permissions().mode() & 0o777;
        if mode & 0o077 != 0 {
            return Err(KeyError::Permissions {
                path: path.to_path_buf(),
                mode,
            });
        }
    }
    fs::read_to_string(path).map_err(io(path))
}

/// Creates `path` with mode 0600 from the start (never briefly world-readable), via a
/// temporary sibling renamed into place.
fn write_private(path: &Path, bytes: &[u8]) -> Result<(), KeyError> {
    let tmp = path.with_extension("tmp");
    let _ = fs::remove_file(&tmp);
    let mut opts = fs::OpenOptions::new();
    opts.write(true).create_new(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        opts.mode(0o600);
    }
    let mut f = opts.open(&tmp).map_err(io(&tmp))?;
    f.write_all(bytes).map_err(io(&tmp))?;
    f.sync_all().map_err(io(&tmp))?;
    drop(f);
    fs::rename(&tmp, path).map_err(io(path))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn mode(p: &Path) -> u32 {
        use std::os::unix::fs::PermissionsExt;
        fs::metadata(p).unwrap().permissions().mode() & 0o777
    }

    #[test]
    fn home_without_keys_gets_them_on_first_run_and_keeps_them() {
        let home = tempfile::tempdir().unwrap();
        assert!(LibraryKeys::load(home.path()).unwrap().is_none());
        let (a, created) = LibraryKeys::load_or_create(home.path()).unwrap();
        assert!(created);
        let (b, created) = LibraryKeys::load_or_create(home.path()).unwrap();
        assert!(!created);
        assert_eq!(a, b);
        let dir = LibraryKeys::dir(home.path());
        assert_eq!(mode(&dir), 0o700);
        assert_eq!(mode(&dir.join(NOSTR_FILE)), 0o600);
        assert_eq!(mode(&dir.join(AGE_FILE)), 0o600);
        assert!(!dir.join("nostr.tmp").exists() && !dir.join("age.tmp").exists());
        let age_file = fs::read_to_string(dir.join(AGE_FILE)).unwrap();
        assert!(age_file.contains(&format!("# public key: {}", a.age_recipient())));
    }

    #[test]
    fn library_id_is_the_first_16_hex_of_the_pubkey() {
        let k = LibraryKeys::generate();
        assert_eq!(k.library_id().len(), 16);
        assert!(k.pubkey_hex().starts_with(&k.library_id()));
        assert!(k.library_id().chars().all(|c| c.is_ascii_hexdigit()));
        assert!(k.npub().starts_with("npub1"));
        assert!(k.age_recipient().starts_with("age1"));
    }

    #[test]
    fn debug_never_shows_a_secret() {
        let k = LibraryKeys::generate();
        let d = format!("{k:?} {:#?}", k);
        assert!(!d.contains(&k.nsec()));
        assert!(!d.contains(&k.age_secret()));
        assert!(!d.contains(&k.nostr().secret_key().to_secret_hex()));
        assert!(d.contains(&k.library_id()));
    }

    #[test]
    fn one_key_file_alone_is_an_error_not_a_new_library() {
        let home = tempfile::tempdir().unwrap();
        LibraryKeys::generate().save(home.path()).unwrap();
        fs::remove_file(LibraryKeys::dir(home.path()).join(AGE_FILE)).unwrap();
        assert!(matches!(
            LibraryKeys::load_or_create(home.path()),
            Err(KeyError::Partial { .. })
        ));
    }

    #[test]
    fn save_never_overwrites() {
        let home = tempfile::tempdir().unwrap();
        let (a, _) = LibraryKeys::load_or_create(home.path()).unwrap();
        assert!(matches!(
            LibraryKeys::generate().save(home.path()),
            Err(KeyError::Exists(_))
        ));
        assert_eq!(LibraryKeys::load(home.path()).unwrap().unwrap(), a);
    }

    #[test]
    fn group_readable_key_file_is_refused() {
        use std::os::unix::fs::PermissionsExt;
        let home = tempfile::tempdir().unwrap();
        LibraryKeys::load_or_create(home.path()).unwrap();
        let p = LibraryKeys::dir(home.path()).join(NOSTR_FILE);
        fs::set_permissions(&p, fs::Permissions::from_mode(0o644)).unwrap();
        let err = LibraryKeys::load(home.path()).unwrap_err();
        assert!(matches!(err, KeyError::Permissions { mode: 0o644, .. }));
    }

    #[test]
    fn from_secrets_round_trips() {
        let k = LibraryKeys::generate();
        assert_eq!(
            LibraryKeys::from_secrets(&k.nsec(), &k.age_secret()).unwrap(),
            k
        );
        assert!(LibraryKeys::from_secrets("nsec1nope", &k.age_secret()).is_none());
        assert!(LibraryKeys::from_secrets(&k.nsec(), "AGE-SECRET-KEY-1NOPE").is_none());
    }
}
