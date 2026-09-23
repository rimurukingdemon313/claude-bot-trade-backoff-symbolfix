# When is this finished?

> **Superseded, September 2026.** This page planned to wait for about
> twenty live trades before judging the strategy. That question has since
> been answered far more strongly than twenty trades ever could:
>
> - SMC: 14,594 historical trades over nine years and twelve instruments,
>   no edge after costs (t = −9.3) and none before them either
>   (`EXPERIMENT_HISTORICAL_EDGE.md`).
> - The built-in `reversion` mode could never trade, and its signal lost on
>   38,971 trades (t = −14.3). It has been removed (`EXPERIMENT_REVERSION.md`).
> - Three published trend-following rules were tested as replacements; none
>   passed out of sample (`EXPERIMENT_TREND_FOLLOWING.md`).
>
> The honest state is that the bot has no strategy with a demonstrated edge,
> and the right action is to pause trading until one passes the same test.
> The engineering below is still accurate; the plan it ends with is not.

Written because the operator asked, after days of daily fixes, when we
stop editing and start running. It is a fair question and it deserves a
straight answer rather than reassurance.

## Why the last few days were like that

Every defect was hiding behind the one in front of it:

1. the dashboard did not show why a setup was refused, so
2. nobody could see the quote endpoint returning 404, which
3. killed every order at the final read and hid the three lockout
   layers behind it, which in turn hid
4. the partial take-profit firing on every poll, which hid
5. the bot being unable to read its own results at all.

That is not five unrelated bugs. It is one blindness with five
symptoms. Each fix made the next one visible, which is why it felt
endless and why it now stops: the system reports what it is doing, so
the next problem announces itself instead of being excavated.

## What "done" means, concretely

Software that touches a live market is never "finished" the way a
document is. But there is a real milestone, and these are its terms:

* **No known defect blocks or corrupts a trade.** Met. Every one found
  has a test that fails against the commit before its fix.
* **The system measures honestly.** Met. It refuses to invent a price,
  a P/L or a statistic, and says so where a value is missing.
* **It explains itself.** Met. Every refusal names the gate that fired;
  every failed execution shows the guard that stopped it.
* **Position management cannot repeat itself.** Met, and verified per
  action: break-even, trailing and the structural exit are all guarded
  by live broker state. The partial was the only one guarded by a flag
  we had to persist ourselves, which is exactly why it was the only one
  that broke.
* **It is profitable.** NOT MET, and not promised. See below.

Four of five are met. The fifth is not an engineering milestone.

## The part that is not engineering

Nobody can make a strategy profitable by editing code. What code can do
is stop a strategy losing money to its own defects — costs it did not
need to pay, winners cut short by a bug, losses the counters never saw.
All of that is now fixed.

Whether SMC on M15 has an edge on this account is a question only its
own trade history can answer. Every measurement in this repository so
far is negative, and one properly pre-registered experiment failed its
own validation. I will not dress that up.

So the honest position: the bot is ready to be TRUSTED TO RUN, not
proven to make money. Those are different claims and only the first is
mine to make.

## The plan that ends the daily editing

1. Set the configuration once (see .env.example).
2. Change NOTHING for two weeks.
3. Do not read individual trades. Three trades are noise; so are ten.
4. After roughly twenty closed trades, read the Stats tab.

Step 2 is the hard one and it is the one that matters. Every
mid-flight change resets the sample and makes the history
uninterpretable — which is the whole reason every trade records a
tuning fingerprint. Changing a setting after eight trades does not give
you eight trades of evidence; it gives you nothing, twice.

If something is genuinely broken it will announce itself on the
dashboard, in words, on the phone already in your hand. That is what
the last few days bought.

## What to look at after twenty trades

Not the balance. These:

* **win rate** against the break-even rate for the average R:R;
* **average R**, which is the only figure that compares across
  account sizes and position sizes;
* **fees as a share of gross P/L** — it was 33% over the first three
  trades, and if it stays near that, the stop floor is too tight and
  `RISK_MIN_STOP_ATR` is the lever;
* **`sample`** — while it reads "insufficient", the numbers above
  describe what happened and are not yet evidence of anything.
