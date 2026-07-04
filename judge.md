# Post scoring rubric

Derived from X's open-source recommendation algorithm (xai-org/x-algorithm,
`home-mixer/scorers/weighted_scorer.rs`). The algorithm predicts engagement
action probabilities and combines them with these weights; the rubric below
redistributes them onto a 100-point scale for text-only judging.

## Positive factors (max 100)

- **Reply potential (0-22)**: does it invite conversation? A take people want
  to respond to, a question worth answering, an observation that begs a "same"
  or a counterexample. Reply weight (1.0) is the highest in the algorithm.
- **Retweet potential (0-16)**: is it sharable as-is? Punchy, self-contained,
  makes the sharer look smart or funny.
- **Like potential (0-12)**: relatable, satisfying, "yes exactly".
- **Quote potential (0-10)**: a strong or slightly contrarian claim people
  want to add commentary to.
- **Dwell time (0-10)**: rewards density, a twist, or a second read. One-line
  platitudes score low; a sentence with a turn scores high.
- **Profile click / follow potential (0-9)**: distinct voice; would a stranger
  think "who is this?" and check the profile.
- **Specificity (0-11)**: concrete details, numbers, named tools, real
  scenarios. Generic abstractions score 0 here.
- **Voice fit (0-10)**: matches the persona; sounds like a person, not a brand.

## Penalties (subtract)

- **AI tells (-0 to -25)**: stock LLM phrasing, forced parallelisms
  ("it's not X, it's Y"), rule-of-three lists, engagement-bait questions
  bolted on the end, hedging.
- **Rage-bait / block risk (-0 to -20)**: the algorithm's block (-1.2) and
  report (-1.5) weights are severe. Punchy is good; insulting a group is not.
- **Generic take (-0 to -15)**: could have been posted by anyone about
  anything; already said a thousand times.

Score = sum of positives + penalties, clamped to 0-100.
