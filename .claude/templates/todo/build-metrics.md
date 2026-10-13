# Build metrics NNN slug

<!-- Written by the /build coordinator while it runs, one row per dispatch or check, as each one closes. Times are local wall clock. Tokens are what the harness reports when an agent finishes. A resumed agent reports its whole context, so its row takes the difference from its previous total and says so. -->

Compared against: <!-- the last measured bundle in _done/, by its Totals table -->

## Baseline

<!-- The profile's commands, timed once on a clean tree before the first dispatch. -->

| command | wall |
|---|---|
| | |

## Layout

| lane | groups, in order |
|---|---|
| | |

## Dispatches and checks

<!-- kind: tests, build, style, reaper, audit, planner, check (your own), other. new: fresh, or resume of row N. Outcome: one line. accepted, bounced, stopped, died, and what caught it. -->

| # | what | kind | lane | new | start | end | wall | tokens | tool uses | outcome |
|---|---|---|---|---|---|---|---|---|---|---|
| | | | | | | | | | | |

## Totals

Elapsed: <!-- session start to last row -->
Agent wall clock, summed: 
Tasks: , criteria: 
Full suite runs: <!-- how many, and what each cost -->

| kind | rows | wall | share of agent time | tokens | share of tokens |
|---|---|---|---|---|---|
| tests | | | | | |
| build | | | | | |
| style | | | | | |
| reaper | | | | | |
| audit | | | | | |
| check | | | | | |
| **total** | | | | | |

## Where it went

<!-- Three to five lines. The longest rows and why, against the bundle you compared with. A number for each claim. -->

## Levers for the next bundle

<!-- At most five, cheapest first, each with the rows that argue for it. A lever that would hold in any repository is worth proposing for the kit. -->
