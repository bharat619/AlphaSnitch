# Breakout risk levels

`breakout-risk-1` calculates the accepted research hypothesis from a retained
breakout assessment and fixed-anchor ATR14 observation:

- Freeze breakout B, signal close C and strategy-ready ATR A.
- Stop S = B - A; target T = C + 3A. Both remain fixed when proposed entry changes.
- Require A > 0, S > 0, E > B and S < E < T.
- With explicit total round-trip allowances c_win and c_loss per execution share,
  planned reward = T - E - c_win and planned loss = E - S + c_loss.
- Require positive planned loss and reward >= 2 × loss. Equality passes.
- The exact upper entry bound is (T + 2S - c_win - 2c_loss) / 3. Round this cap
  down to the declared price increment. The first valid entry is the first tick
  strictly above B. An empty tick range fails.

All arithmetic uses exact rational values from normalized Decimal inputs, including
the canonically rounded ATR supplied by the existing ATR contract. Comparison
values serialize as rational strings; they must not be replaced by rounded display
values. The arithmetic does not depend on the ambient Decimal precision or traps.

For B=101, C=102, A=2 and c_win=c_loss=0.10, S=99 and T=108. Entries 101.80 and
101.90 pass the ratio; 101.900001, 102 and 103 fail. At a 0.01 increment,
101.900001 also fails the tick check. The range is 101 < E <= 101.90.

## Inputs and source authentication

`assess_risk` authenticates the retained breakout via `replay_breakout` and the ATR
via its original source replay. The ATR must have completed its 245 warm-up updates
and match the signal snapshot, session, cutoff and publication/volume provenance.
An altered ATR value cannot improve the ratio. Unavailable or incompatible ATR
leaves levels unavailable. Missing costs, entry or mapping have explicit reasons;
independently known failures remain visible even when aggregate status is UNAVAILABLE.

The result embeds these inputs, the versioned policy/hash, comparisons, canonical
failure order, exact values and the intended next exchange session. Result JSON
validation checks internal consistency; `replay_risk` additionally authenticates
the original price files and calendar again. Reordered JSON object keys are valid.
Recalculation at a later time retains the original signal cutoff and frozen levels.

## Execution basis and price increments

`ExecutionPriceMapping` is a caller-supplied, reviewed assertion binding the
instrument, snapshot hash, ATR adjustment revision, execution session and currency.
It supplies an exact positive numerator/denominator multiplier, execution-basis
identifier, positive tick increment, availability time and verification reference.
An identity conversion also needs this evidence; no factor of 1 is inferred.

The factor converts B, S and T into execution-date price units. It can describe
split adjustments; currency conversion, dividends/total-return conversions and
additive transformations are outside this version. The record's verification flag
and reference do not independently prove an external corporate action. The caller
must supply evidence supporting the complete conversion and execution tick. The
generic instrument precision field is not assumed to be a verified market tick.

The calendar resolves the first scheduled session after the signal, searching the
next 32 calendar dates. Missing sessions block the calculation; malformed or
out-of-order schedules are rejected. Mapping and costs must be available at the
signal cutoff and match that session. An assessment cutoff at or after its open
is unavailable. The separate earnings/timing policy retains its explicit pre-open
buffer; this risk-only calculation does not replace it or validate a live quote.

The provisional `require_aligned` policy preserves exact stop and target levels.
If either falls between valid ticks, executable levels remain UNAVAILABLE with
`EXECUTABLE_LEVEL_ROUNDING_UNRESOLVED`. No stop/target rounding convention has been
approved. The specified entry cap is still floored to a tick and retained for
inspection, alongside that blocker.

## Cost scope and separate outcomes

`FixedCostAllowances` requires explicit nonnegative winning-exit and losing-exit
allowances, including all entry and exit fees/slippage for each outcome once.
They are expressed per share in the execution basis, so the price mapping does
not rescale them. Zero is accepted only when supplied explicitly. The record binds
the instrument, currency, basis, session, availability and assumptions reference.
Different winning and losing allowances are supported. No fee schedule or slippage
calibration is inferred. Quantity-dependent commissions and minimum fees require
a separate quantity-aware model and are unsupported by this fixed-per-share model.

Risk status describes these calculations only. Technical setup failures remain in
the embedded breakout assessment; earnings, source qualification, account limits,
advisory sizing and recommendation composition remain separate. A risk PASS alone
does not clear those gates. Recommendation status remains NOT_EVALUATED. A stop
trigger and modeled loss do not promise an execution price or maximum actual loss.

## Executable walkthrough

Use the locked environment with this checkout's `src` on `PYTHONPATH`:

```sh
python -m stock_analyser.risk_example --output /path/to/new-risk-example
```

The first section prints the accepted B=101 arithmetic example. The second freezes
260 synthetic bars and calculates authenticated B=359, C=360, A=2, S=357 and T=366.
It retains and replays five JSON assessments: a passing 359.90 entry, a failing
360 entry, missing costs, unverified mapping, and off-tick levels. Its prices,
costs, mapping and enumerated schedule are synthetic assertions. No provider,
broker or model calls occur. Tests also cover split conversion, strict boundaries,
cost identities, future evidence, corruption, JSON round trips and fresh-process
reproduction.
