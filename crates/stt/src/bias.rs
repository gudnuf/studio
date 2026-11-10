/// Assemble the whisper `initial_prompt` from the user's vocabulary terms
/// (memory `vocabulary` section, spec §vocabulary point 3). Spike `RESULTS.md`:
/// initial_prompt injection gave +10–19 pp term recall with zero hallucination.
/// This is the v1 biasing SEAM — a later plan swaps in trie/logit-bias by
/// replacing what `SttStream` does with these terms, not this signature.
pub fn build_bias_prompt(terms: &[String], max_terms: usize) -> Option<String> {
    let kept: Vec<&str> = terms.iter().take(max_terms).map(String::as_str).collect();
    if kept.is_empty() {
        return None;
    }
    // A glossary-style list; whisper reads the prompt as prior context, so a
    // natural comma list biases toward these spellings without a rigid schema.
    Some(format!("Terms used in this session: {}.", kept.join(", ")))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn empty_terms_yield_no_prompt() {
        assert_eq!(build_bias_prompt(&[], 100), None);
    }

    #[test]
    fn terms_are_joined_and_capped() {
        let terms: Vec<String> = (0..150).map(|i| format!("term{i}")).collect();
        let p = build_bias_prompt(&terms, 100).unwrap();
        assert!(p.contains("term0") && p.contains("term99"));
        assert!(
            !p.contains("term100"),
            "capped at max_bias_terms (spec ≤100)"
        );
    }
}
