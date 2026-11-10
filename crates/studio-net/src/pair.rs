//! The pairing payload: everything a second device needs to join the library. v1 carries the
//! library's secrets themselves (one shared key, operator decision 2026-11-09), so the payload
//! is as sensitive as `keys/`: it goes out only as a terminal QR or on explicit request.
//!
//! Format: `studio-pair:` + base64url (no padding) of JSON
//! `{"v":1,"nsec":"nsec1…","age":"AGE-SECRET-KEY-1…","relays":[…],"blossom":[…]}`.

use std::fmt;

use base64::Engine as _;
use serde::{Deserialize, Serialize};
use zeroize::{Zeroize, Zeroizing};

use crate::config::{check_blossom, check_relay};
use crate::keys::LibraryKeys;

pub const PREFIX: &str = "studio-pair:";
pub const VERSION: u32 = 1;

#[derive(Debug, thiserror::Error)]
pub enum PairError {
    #[error("not a studio pairing payload (expected it to start with {PREFIX})")]
    NotAPayload,
    #[error("pairing payload is damaged (bad base64 or JSON)")]
    Damaged,
    #[error("pairing payload version {0} is newer than this studio understands ({VERSION})")]
    Version(u32),
    #[error("pairing payload holds invalid keys")]
    BadKeys,
    #[error(transparent)]
    Url(#[from] crate::config::ConfigError),
    #[error("pairing payload is too long for a QR code (too many relay/Blossom URLs)")]
    TooLong,
}

#[derive(Clone, PartialEq, Eq)]
pub struct Pairing {
    pub keys: LibraryKeys,
    pub relays: Vec<String>,
    pub blossom: Vec<String>,
}

impl fmt::Debug for Pairing {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("Pairing")
            .field("keys", &self.keys)
            .field("relays", &self.relays)
            .field("blossom", &self.blossom)
            .finish()
    }
}

#[derive(Serialize, Deserialize)]
struct Wire {
    v: u32,
    nsec: String,
    age: String,
    #[serde(default)]
    relays: Vec<String>,
    #[serde(default)]
    blossom: Vec<String>,
}

impl Drop for Wire {
    fn drop(&mut self) {
        self.nsec.zeroize();
        self.age.zeroize();
    }
}

impl Pairing {
    /// The payload text. It contains the library's secrets: never log it.
    pub fn encode(&self) -> Zeroizing<String> {
        let wire = Wire {
            v: VERSION,
            nsec: self.keys.nsec().to_string(),
            age: self.keys.age_secret().to_string(),
            relays: self.relays.clone(),
            blossom: self.blossom.clone(),
        };
        let json = Zeroizing::new(serde_json::to_vec(&wire).expect("serialize pairing"));
        let b64 = Zeroizing::new(base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(&*json));
        Zeroizing::new(format!("{PREFIX}{}", b64.as_str()))
    }

    pub fn decode(text: &str) -> Result<Self, PairError> {
        let body = text
            .trim()
            .strip_prefix(PREFIX)
            .ok_or(PairError::NotAPayload)?;
        let json = Zeroizing::new(
            base64::engine::general_purpose::URL_SAFE_NO_PAD
                .decode(body)
                .map_err(|_| PairError::Damaged)?,
        );
        // Peek at the version first so a newer payload says so instead of "damaged".
        #[derive(Deserialize)]
        struct V {
            v: u32,
        }
        let v: V = serde_json::from_slice(&json).map_err(|_| PairError::Damaged)?;
        if v.v != VERSION {
            return Err(PairError::Version(v.v));
        }
        let mut w: Wire = serde_json::from_slice(&json).map_err(|_| PairError::Damaged)?;
        let keys = LibraryKeys::from_secrets(&w.nsec, &w.age).ok_or(PairError::BadKeys)?;
        w.relays.iter().try_for_each(|r| check_relay(r))?;
        w.blossom.iter().try_for_each(|b| check_blossom(b))?;
        Ok(Self {
            keys,
            relays: std::mem::take(&mut w.relays),
            blossom: std::mem::take(&mut w.blossom),
        })
    }

    /// The payload as a terminal QR (unicode half blocks, quiet zone included).
    /// Dark modules are drawn as blanks, light ones as blocks: right on a dark terminal,
    /// inverted on a light one (iOS Camera reads both).
    pub fn qr(&self) -> Result<String, PairError> {
        let payload = self.encode();
        let code =
            qrcode::QrCode::with_error_correction_level(payload.as_bytes(), qrcode::EcLevel::L)
                .map_err(|_| PairError::TooLong)?;
        Ok(code
            .render::<qrcode::render::unicode::Dense1x2>()
            .dark_color(qrcode::render::unicode::Dense1x2::Light)
            .light_color(qrcode::render::unicode::Dense1x2::Dark)
            .build())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sample() -> Pairing {
        Pairing {
            keys: LibraryKeys::generate(),
            relays: vec!["ws://nous.local:7777".into(), "wss://relay.example".into()],
            blossom: vec!["http://nous.local:3000".into()],
        }
    }

    #[test]
    fn payload_round_trips() {
        let p = sample();
        let text = p.encode();
        assert!(text.starts_with(PREFIX));
        let back = Pairing::decode(&text).unwrap();
        assert_eq!(back, p);
        assert_eq!(back.keys.library_id(), p.keys.library_id());
        // whitespace from a copy/paste is fine
        assert_eq!(
            Pairing::decode(&format!("  {}\n", text.as_str())).unwrap(),
            p
        );
    }

    #[test]
    fn rejects_what_is_not_a_v1_payload() {
        let b64 = |s: &str| base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(s);
        assert!(matches!(
            Pairing::decode("hello"),
            Err(PairError::NotAPayload)
        ));
        assert!(matches!(
            Pairing::decode("studio-pair:%%%"),
            Err(PairError::Damaged)
        ));
        assert!(matches!(
            Pairing::decode(&format!("{PREFIX}{}", b64(r#"{"v":2}"#))),
            Err(PairError::Version(2))
        ));
        let k = LibraryKeys::generate();
        let bad = format!(
            r#"{{"v":1,"nsec":"nsec1x","age":"{}"}}"#,
            k.age_secret().as_str()
        );
        assert!(matches!(
            Pairing::decode(&format!("{PREFIX}{}", b64(&bad))),
            Err(PairError::BadKeys)
        ));
        let bad_url = format!(
            r#"{{"v":1,"nsec":"{}","age":"{}","relays":["http://x"]}}"#,
            k.nsec().as_str(),
            k.age_secret().as_str()
        );
        assert!(matches!(
            Pairing::decode(&format!("{PREFIX}{}", b64(&bad_url))),
            Err(PairError::Url(_))
        ));
    }

    #[test]
    fn qr_renders_and_does_not_spell_the_secret() {
        let p = sample();
        let q = p.qr().unwrap();
        assert!(q.lines().count() > 20);
        assert!(!q.contains(p.keys.nsec().as_str()) && !q.contains(p.keys.age_secret().as_str()));
    }

    #[test]
    fn an_oversized_payload_is_an_error_not_a_panic() {
        let mut p = sample();
        p.relays = (0..200)
            .map(|i| format!("wss://relay-{i}.example.org/some/long/path"))
            .collect();
        assert!(matches!(p.qr(), Err(PairError::TooLong)));
    }

    #[test]
    fn debug_never_shows_a_secret() {
        let p = sample();
        let d = format!("{p:?}");
        assert!(!d.contains(p.keys.nsec().as_str()) && !d.contains(p.keys.age_secret().as_str()));
        assert!(!d.contains(p.encode().as_str()));
    }
}
