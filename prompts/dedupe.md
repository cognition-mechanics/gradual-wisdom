DEDUPE_ADJUDICATION

You are cleaning a long-term memory of observations about a user and about the assistant's own behaviour. Many entries restate the same thing in different words. For each entry you get a short list of similar-looking candidates from the same category. Decide which candidates are **duplicates**: they express the same underlying fact, preference, lesson or theme.

Rules:
- A duplicate makes the same claim, even if worded differently or more or less specific.
- Sharing a topic is not enough. "Uses Azure DevOps for PRs" and "Uses Azure DevOps pipelines for deployment" are different claims. Opposite claims are never duplicates.
- False merges destroy information, missed merges only waste space. When in doubt, leave it out.
- Apply this test: would someone who knows only the stored text already know the new claim (or vice versa)? If the two describe different aspects of the same topic, the answer is no.
  Example: "Prefers dark editor themes" vs "Prefers a 14pt editor font": both about the editor, different claims → not the same.
- Only answer with ids from that entry's own candidate list.

Input: a JSON list of `{"key": "<entry id>", "category", "observation", "candidates": [{"id", "text"}]}`.

Output: ONLY a JSON object, with no prose and no code fence:
{"duplicates": {"<entry id>": ["<candidate id>", ...], ...}}
Use an empty list when an entry has no duplicates.
