MATCH_ADJUDICATION

You maintain a long-term memory of observations about a user and about the assistant's own behaviour. New observations arrive each day, phrased differently from the stored ones. Your job is to decide, for each new observation, whether it expresses **the same underlying fact, preference, lesson or theme** as one of its listed candidates.

Rules:
- Match only when they are the same claim, even if worded differently, more or less specific, or in a different tone. "Prefers terse answers" and "Wants short replies without padding" match.
- Do NOT match when they merely share a topic but make different claims. "Uses Azure DevOps for PRs" and "Uses Azure DevOps pipelines for deployment" do not match. Neither do opposite claims.
- When several candidates fit, pick the closest one.
- When none fits, answer null. False matches are worse than missed ones.
- Apply this test: would someone who knows only the stored text already know the new claim (or vice versa)? If the two describe different aspects of the same topic, the answer is no.
  Example: "Prefers dark editor themes" vs "Prefers a 14pt editor font": both about the editor, different claims → not the same.
- Only answer with ids from that observation's own candidate list.

Input: a JSON list of `{"key", "category", "observation", "candidates": [{"id", "text"}]}`.

Output: ONLY a JSON object, with no prose and no code fence:
{"matches": {"<key>": "<candidate id>" | null, ...}}
Include every key from the input.
