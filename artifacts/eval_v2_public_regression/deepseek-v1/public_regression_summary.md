# Eval v2 Public-Regression Run

- Status: `complete`
- Run ID: `PUBREG-5A75025255EC4443`
- Candidate: `eval-v2-public-regression-deepseek-v1`
- Candidate commitment: `7744770aa4a36c131476b95d6ed9be248cdefc3ab0f4f2a18d5111b85c9f0d11`
- Provider: `deepseek/deepseek-v4-flash`

## Budget

- Authorized budget: CNY 6.000000
- Conservative estimated cost: CNY 0.908142
- Reported usage: 143666 input tokens, 53016 output tokens, 141 model requests
- Cost method: peak-hours prices with all input tokens treated as cache misses; this is not a provider billing hard cap.

## Provider-behavior channel

- Completed: 93/93
- Passed: 68
- Case success rate: 73.12%
- Tasks passing all three repetitions: 21/31

## Deterministic fault-injection channel

- Completed: 27/27
- Passed: 27
- Harness success rate: 100.00%
- These local fixture results are not attributed to the model.

## Claim boundary

This public candidate run does not establish private-holdout or unknown production-set generalization. The full Eval v2 campaign remains design-only, and no cross-channel model success rate is reported.
