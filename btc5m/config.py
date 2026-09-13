"""
Every setting the bot has, and the rules that make a set of them
coherent enough to trade on.
"""

from __future__ import annotations

from dataclasses import dataclass

from btc5m.constants import DEFAULT_ROUND_SECONDS
from btc5m.pricing import max_price_for_return
from btc5m.venue.endpoints import DEFAULT_ENDPOINTS

@dataclass(frozen=True)
class Config:
    api_key: str
    api_secret: str
    live: bool = True

    # --- Edge --------------------------------------------------------------
    # Two thresholds, both of which must clear. The absolute floor stops us
    # trading noise; the ratio keeps the bar consistent across price levels.
    # An absolute 0.06 edge is a 7% margin at price 0.90 but a 60% margin at
    # 0.10 -- one number cannot serve both ends of the book.
    min_edge: float = 0.02
    min_edge_ratio: float = 0.30      # model_prob >= breakeven * 1.30
    # How far spot must sit from the strike, measured in standard deviations
    # of the REMAINING time. This is "big buffer AND late in the round"
    # expressed as one number: the same buffer is worth more with less time
    # left, and z captures both at once. 0 disables the gate.
    min_buffer_sigmas: float = 0.0
    # Minimum NET profit per unit staked on a win, after the venue's fee.
    #
    # A positive edge says a bet is worth making; it says nothing about
    # whether the payout is worth the risk of ruin attached to it. Buying at
    # 0.94 returns about 6% on a win, so one loss erases sixteen wins. The
    # arithmetic is unforgiving in a way the edge test cannot see, because
    # edge is measured in probability and this is measured in money.
    #
    # 0.25 means "a win must pay at least 25% of the stake", which caps the
    # fill price at (1-fee)/((1-fee)+0.25) -- about 0.797 at a 2% fee. The
    # cap is derived from the fee rather than written down, so a market with
    # a different fee gets a different ceiling automatically.
    #
    # 0.0 disables it and the entry band alone governs.
    min_win_return: float = 0.0
    # FALLBACK only. Each market publishes its own feeRateBps and that value
    # wins. Assuming 2% when the real rate is near zero silently demands about
    # a point of extra edge that does not exist, and suppresses valid trades.
    fee_bps: int = 200
    # Deliberately permissive defaults. Every profile sets these explicitly;
    # a convex-shaped default silently narrowed any custom config that did not.
    max_entry_price: float = 0.95
    min_entry_price: float = 0.05
    # Haircut on an indicative quote when the book cannot be read, as a
    # FRACTION of the price. A flat 0.03 is 60% of a 0.05 longshot but 3% of
    # a 0.95 near-certainty -- one absolute number cannot serve both ends.
    assumed_spread_pct: float = 0.05
    max_price_impact: float = 0.05      # reject quotes that move the book far

    # --- Execution ---------------------------------------------------------
    # How an entry reaches the book. MARKET crosses the spread and is
    # fill-or-kill; LIMIT rests at the model's own reservation price and may
    # fill in pieces or not at all.
    #
    # Per profile, because it is a strategy decision and not plumbing: a
    # profile that lives on thin longshots and one that buys favourites late
    # want opposite answers, and one global setting would be wrong for one of
    # them whichever way it was set.
    entry_order_type: str = "MARKET"
    # Whether to leave a position before it settles, and how.
    #
    # NONE is what this bot has always done: hold to settlement and redeem
    # the winning token. Stated rather than implied, so a profile that wants
    # exits has to say so and a profile that does not cannot acquire them by
    # a default changing underneath it.
    #
    # MARKET and LIMIT both price the exit off the MODEL: leave when the book
    # overpays by the same edge the entry demanded. BRACKET does not, and is
    # therefore its own value rather than a flavour of LIMIT -- the scalp
    # strategy's exit prices come from the price it FILLED at and the P&L it
    # is willing to take, and the model has no opinion about either. Folding
    # it into LIMIT would mean exit_trigger silently applied to a mechanism
    # it does not describe.
    exit_order_type: str = "NONE"
    # RESTING posts the sell when the entry fills and lets the venue wait.
    # POLLED recomputes the reservation price each loop and sends only once
    # the bid crosses it. Inert while exit_order_type is NONE.
    exit_trigger: str = "RESTING"

    # --- Timing ------------------------------------------------------------
    entry_window_start_s: int = 150
    entry_window_end_s: int = 25

    # --- Risk --------------------------------------------------------------
    kelly_fraction: float = 0.25
    max_stake_pct: float = 0.05
    # Small accounts: allow betting the venue minimum when Kelly sizes below
    # it, bounded by the 2x-full-Kelly limit inside kelly_stake.
    round_up_to_minimum: bool = True
    # Absolute ceiling for the small-account override, as a MULTIPLE of
    # max_stake_pct rather than an independent number. Expressed absolutely
    # it had to be kept >= max_stake_pct by every caller, and any config that
    # raised one without the other failed validation. As a multiple the
    # invariant holds by construction and cannot be violated.
    hard_stake_multiple: float = 2.5
    hard_stake_ceiling: float = 0.35
    # Scale-in: start small, then top up as the round moves in our favour.
    # This is NOT martingale -- martingale adds after LOSSES, chasing. This
    # adds only when the position is winning and the model's probability has
    # risen, and it tops up toward the Kelly stake for the CURRENT
    # probability rather than stacking independent bets. Total exposure to a
    # single round is therefore still governed by Kelly, not multiplied by it.
    # Add unclaimed winnings to the live bankroll?
    #
    # Off by default. The portfolio's totalCurrentValue already counts settled
    # positions, so adding the gross payout on top double-counts it -- and the
    # error is the GROSS payout (stake + profit), not the profit, so a 1.00
    # stake winning at 0.60 inflated the reported bankroll by 1.67 rather than
    # 0.65. Turn it on only if reconciliation shows the API genuinely excludes
    # unredeemed winnings.
    count_unredeemed_in_bankroll: bool = False
    # Confirm every live order actually FILLED before recording a position.
    #
    # PlaceOrderResponse carries only an orderId -- nothing about fills. With
    # timeInForce=FOK an order that cannot fill is KILLED, and the venue still
    # returns an id. Trusting that id books a position that never existed,
    # which then "settles" and reports a profit that was never made. The only
    # way to know is to ask the venue.
    confirm_fills: bool = True
    fill_confirm_attempts: int = 5
    fill_confirm_delay_s: float = 1.0
    # A fill this far below the amount requested is treated as a failure
    # rather than a partial position.
    min_fill_fraction: float = 0.90
    # Warn when the API balance moves by more than this fraction away from the
    # P&L we expected. Catches double-counting, unexpected fees and silent
    # partial fills.
    reconcile_tolerance: float = 0.10
    scale_in: bool = False
    scale_in_initial_pct: float = 0.4     # first tranche, as a share of target
    scale_in_min_topup: float = 1.0       # skip top-ups below the order min
    # Ceiling on the BLENDED fill price across all tranches of one round.
    #
    # This is the knob that controls "how many wins does it take to cover a
    # loss", because that number is 1/((1-p)/p) at the blended price p and
    # nothing else. Top-ups happen when the round has moved our way, which
    # means the price has RISEN -- so every top-up drags the blended price up
    # and the payout down. Left unbounded, a large top-up at 0.95 can turn a
    # 6-wins-per-loss position into a 15-wins-per-loss one.
    #
    # A top-up is trimmed to whatever keeps the blend under this cap, and
    # skipped if that leaves less than the minimum order.
    #
    # Meaningful only relative to a profile's own band, so every profile sets
    # it: 0.90 suits buffer (0.80-0.97) and is inert for convex (0.05-0.35),
    # where no fill could ever approach it. Validated against the band below.
    max_blended_price: float = 0.90
    # The connector documents ~1.5 USDT as an APPROXIMATE MARKET-order
    # minimum that "varies by market depth"; the account minimum is 1.00.
    # Small MARKET orders may be rejected on a thin book -- that surfaces as a
    # quote error, not a silent loss.
    min_stake_usdt: float = 1.0
    daily_loss_limit_pct: float = 0.20
    # A low-win-rate strategy produces long losing streaks by design, so a
    # raw streak counter is the wrong instrument -- at a 25% hit rate, four
    # losses in a row happens roughly every five trades. The real question is
    # whether results are significantly worse than the model predicted, which
    # calibration_z answers. The streak cap remains only as a crude backstop.
    max_consecutive_losses: int = 30
    calibration_min_samples: int = 30
    calibration_z_halt: float = -2.5
    max_rounds_per_day: int = 200
    max_consecutive_errors: int = 20
    # Seconds between repeats of an unchanged "WAITING ... | why" line. A new
    # reason is always logged at once; this only paces the reminders that
    # the old one still holds. 0 logs changes only.
    decision_log_interval_s: float = 30.0

    # --- Venue -------------------------------------------------------------
    # chainId, collateral, fee and the venue's own slippage all come from the
    # market payload. Only the values below are genuinely our decisions.
    round_seconds: int = DEFAULT_ROUND_SECONDS
    max_slippage_bps: int = 300          # OUR risk cap; venue default is 1200
    min_liquidity: float = 0.0           # skip markets thinner than this
    account_type: str = "AUTO"           # AUTO | SPOT | FUNDING
    # AUTO derives it from where the balance actually sits. Hardcoding "MPC"
    # while paying from a SPOT/FUNDING account is self-contradictory: those
    # are CEX accounts, and the venue rejects the combination (-3026).
    funding_source: str = "AUTO"         # AUTO | MPC | CEX
    auto_fund_transfer: bool = True      # move collateral for CEX-funded orders
    open_statuses: tuple[str, ...] = ("REGISTERED", "OPEN", "ACTIVE")
    tradable_status: str = "OPEN"
    # Markets to trade. Each is treated as an independent instrument: its own
    # position slot, its own loss streak, its own calibration statistics and
    # its own volatility and tail estimates. What CANNOT be isolated is the
    # bankroll -- there is one account -- so concurrency is capped and sizing
    # runs against uncommitted funds, or N markets quietly stack to N times
    # the intended exposure.
    #
    # Empty (the default) means NO restriction: every 5-minute up/down market
    # the venue lists is discovered and traded automatically, capped only by
    # max_concurrent_positions. Set one or more tickers to trade only those.
    symbols: tuple[str, ...] = ()
    max_concurrent_positions: int = 2
    # Reserve, as a fraction of bankroll, kept free regardless of how many
    # markets look attractive at once.
    reserve_pct: float = 0.30

    # --- Model -------------------------------------------------------------
    # Fat tails, estimated from realised kurtosis at runtime. None => Gaussian.
    use_fat_tails: bool = True
    tail_df_floor: float = 2.5
    tail_df_ceiling: float = 30.0
    # Sigma only needs a short window; kurtosis needs a long one (its standard
    # error is ~sqrt(24/n), so 60 samples cannot distinguish fat tails from
    # noise at all). Fetch long, measure sigma on the recent tail of it.
    vol_lookback_min: int = 500
    sigma_window_min: int = 60
    # The floor exists only to avoid degenerate maths, so it must sit well
    # below any plausible real value. If it ever binds, the model is asserting
    # a volatility rather than measuring one -- and an overstated sigma
    # inflates exactly the tail probabilities the convex profile buys, by up
    # to 3.5x in observed cases. Trading is refused rather than distorted.
    vol_floor_annual: float = 0.03
    vol_ceiling_annual: float = 3.00
    halt_on_clamped_sigma: bool = True

    # --- Trend (inertia) ---------------------------------------------------
    # BTC often runs in one direction for several consecutive rounds. While
    # that lasts, a trade taken early -- before the buffer is large enough
    # for the venue to have repriced -- is both cheaper and more likely to
    # win. That is the whole opportunity, and it is also the whole risk: the
    # run can end on the very round you size up on, and a market oscillating
    # around the strike produces a sequence of one-round "runs" that are pure
    # noise. Every setting below exists to tell those two apart.
    trend_follow: bool = False
    # Minutes of 1m history the trend is measured over. Read from the same
    # klines the volatility estimate already fetches, so this costs nothing.
    trend_lookback_min: int = 30
    # THE PRIMARY GATE. Size of the move over the last round-length, in
    # standard deviations of one such block. This is what fires at the START
    # of a trend: a thrust happening right now scores high on its first
    # block, whereas a count of completed rounds cannot say anything until
    # the move is already old and the price already bad.
    trend_min_impulse: float = 1.2
    # Blocks in the same direction. The MINIMUM is 1 on purpose -- a strong
    # first thrust is a trend beginning, and demanding corroboration means
    # systematically entering late.
    trend_min_run: int = 1
    # The maximum is the brake. A run that has already gone this far is
    # nearer its end than its beginning, and this is the mechanical version
    # of "keep an eye on it as it fades": the boost switches itself off
    # rather than waiting for a loss to switch it off.
    trend_max_run: int = 5
    # Current block's size relative to the previous one. Below this the move
    # is giving back momentum block over block and is dying, however
    # impressive its history looks.
    trend_decay_floor: float = 0.55
    # Projected rounds of life left before the move decays under the impulse
    # floor. 1.0 means "must survive the round I am about to enter", which is
    # the only horizon an entry actually cares about. Raising it demands the
    # move outlast the round with margin.
    trend_min_rounds_left: float = 1.0
    # Net move over the run, in sigmas of the run. Secondary to impulse:
    # it confirms the move is real rather than announcing it.
    trend_min_z: float = 0.8
    # Net displacement divided by the total distance travelled, over the run.
    # A market swinging across the strike covers a lot of ground and ends up
    # nowhere, which scores near zero here and is exactly the case to sit out.
    trend_min_efficiency: float = 0.35
    # How much larger the stake may be while the trend is confirmed AND
    # points the same way as the trade. Still bounded by the hard stake cap
    # and by twice full Kelly, so the boost cannot reach a size that loses
    # money in the long run.
    trend_stake_multiple: float = 1.5
    # How much earlier entry is allowed while the trend is confirmed, in
    # seconds added to entry_window_start_s. Entering early is what makes the
    # price worth having; the buffer gate still has to clear, and because it
    # is measured in sigmas of the REMAINING time, clearing it early takes a
    # genuinely larger move rather than a more lenient test.
    trend_early_entry_s: int = 90

    # --- Straddle (buy both sides at round-open) ----------------------------
    # A different strategy entirely: no probability model, no picking a
    # side. The edge here is that the venue's own pricing sometimes has not
    # converged to a fair ~50/50 in the first seconds of a round, so buying
    # BOTH outcomes locks in a profit whichever way it resolves -- PROVIDED
    # the prices actually paid support that. Every live round is entered
    # (subject only to the worst-case check below): skipping a round on a
    # directional hunch is exactly the judgement this mode does not make.
    straddle: bool = False
    # Stake on EACH side, as a fraction of bankroll. Total committed to one
    # round is roughly double this number, not this number itself.
    straddle_stake_pct: float = 0.05
    # How long after a round opens a FIRST leg may still be started.
    #
    # This window buys RUNWAY, not cheapness. The two legs are bought at
    # different moments and only the pair is a straddle, so the scarce thing
    # is not the entry price -- it is the time left to find the other side
    # after the first is on the book. An opener at t=240 of a 300s round has
    # 60 seconds to be hedged in and usually is not; one at t=30 has four
    # minutes. Open inside the first minute, then spend the rest of the
    # round completing.
    #
    # The window and straddle_first_leg_max_price are one decision, not two.
    # A minute in, spot has barely left the strike and both sides still
    # price near 0.50, so a 0.25 ceiling here would point the opener at the
    # one stretch of the round where its entry price cannot occur -- which
    # is the dead-bot failure a 15s window used to cause, reintroduced from
    # the other end. The ceiling is loosened to match (see below); paying
    # 0.40 for four minutes of completion time is the trade being made.
    straddle_entry_window_s: float = 60.0
    # Optional per-leg sanity ceiling. 1.0 means no ceiling: every round in
    # the window is taken at whatever price is on offer, deliberately,
    # because this strategy's premise is that direction does not matter and
    # skipping rounds on a price judgement is a different strategy. Set
    # below 1.0 only if you want to opt into refusing a leg priced above a
    # level you have decided is too rich.
    straddle_max_leg_price: float = 1.0
    # Off by default. When enabled, a round is only taken if the WORSE of
    # the two possible outcomes still returns at least this fraction of the
    # total staked -- i.e. a hard requirement that the round be a mechanical
    # arbitrage before it is touched. Left off by default because that
    # requirement is what would make the bot refuse most rounds; whether
    # round-open mispricing is real enough to trade without it is exactly
    # what --calibration-report against real results answers, not a formula
    # decided in advance.
    straddle_require_positive_worst_case: bool = False
    straddle_min_worst_case_return: float = 0.0

    # --- Legging in: the two sides are bought at DIFFERENT times ----------
    # The prices of the two sides sum to about 1.00 at any given instant, so
    # a pair bought simultaneously is almost never profitable both ways.
    # Bought at different moments it can be: UP at 0.25 while spot is falling
    # and DOWN at 0.25 twenty seconds later once it has bounced never coexist
    # in the book, but the two fills together still return 4x on each side of
    # a round that cost 2 stakes. Prices summing to 1.00 constrains one
    # instant, not one round.
    #
    # The most the OPENING leg may cost. This governs the first leg only:
    # what the second holds out for is derived from what the first actually
    # filled at, not from this number (_completion_is_worth_waiting_out).
    # Which side it happens to be is never considered -- whichever of UP and
    # DOWN is showing a price under this is the one that gets bought.
    #
    # 0.25 -- a 4x payout -- is the price this strategy would LIKE, and it
    # is still what the second leg is measured against once a leg fills
    # there. It is the wrong ceiling for a first minute, though: 0.25 does
    # not exist that early, so a bot holding out for it opens nothing. 0.40
    # is what an early book actually offers.
    #
    # The number matters more than it looks. After filling at p, the other
    # side stays worth buying all the way up to (1 - p), so an opener at
    # 0.40 leaves 0.60 of room to complete in, while one at 0.49 leaves
    # almost none and stands a real chance of being stranded. That room,
    # against the four minutes the shortened entry window leaves to use it,
    # is the whole reason 0.40 is tolerable and 0.49 is not.
    straddle_first_leg_max_price: float = 0.40
    # Opening a round is limited to straddle_entry_window_s. COMPLETING one
    # is not: a hedge that locks the profit in at t=120s is worth exactly
    # what one at t=9s is worth, and refusing it would leave a naked
    # directional bet on the book for no reason. The second leg is hunted
    # until this many seconds remain, at which point the search stops and
    # straddle_force_hedge decides what happens to the open leg.
    straddle_hedge_deadline_s: float = 30.0
    # At that deadline, buy the other side at whatever it costs. The round is
    # then usually a small loss instead of a coin flip on the whole stake --
    # the guarantee is already gone by this point, and the only question left
    # is whether the position stays all-or-nothing. Turn off only to hold the
    # open leg to settlement as an outright directional bet.
    straddle_force_hedge: bool = True

    # --- Last minute (buy whichever side the book has already picked) -----
    # A third entry strategy, and the simplest thing in this file. It reads
    # no model, no volatility, no buffer and no trend. With a minute left it
    # looks at the two asks, buys the dearer one -- the side the book is
    # calling the winner -- and does nothing else for the rest of the round.
    #
    # The premise is that a price is a forecast, and in the last minute of a
    # five-minute round it is a forecast with almost no time left in which to
    # be wrong. That is a claim about this venue's late pricing, not about
    # BTC, and --calibration-report's favourite-longshot table is the thing
    # that settles it. Nothing here is derived from the model, so nothing
    # here can be defended by the model either.
    last_minute: bool = False
    # When the hunt opens, in seconds before settlement.
    last_minute_start_s: float = 60.0
    # The price the leading side must show to be bought on sight.
    #
    # Above 0.5 by construction: at or below it the "dearer side" test and
    # the floor would say the same thing, the floor could never fail, and
    # the fallback below would be dead code wearing the costume of a setting.
    last_minute_price_floor: float = 0.75
    # Never pay more than this for the leader. 1.0 means NO ceiling, which
    # is the rule as originally specified and the default here.
    #
    # It exists because the floor has nothing above it, and that asymmetry is
    # where this strategy bleeds. The floor is a payout question wearing a
    # price: at 0.75 a win pays about 33% and 3 wins cover a loss, but at
    # 0.97 a win pays about 3% and it takes 32. Those are not the same trade,
    # and the second one is what a round that is ALREADY DECIDED at 55
    # seconds looks like -- so the dear fills are exactly the ones with the
    # least left to win and the most already priced in.
    #
    # Deliberately a price and not a min_win_return: the return floor is the
    # model path's instrument and it derives its cap from each market's fee,
    # which is the right shape for a strategy that reasons in probabilities.
    # This one reasons in nothing but the quote, so its ceiling is a quote.
    #
    # 0.90 (about 9 wins per loss) or 0.85 (about 6) are the settings worth
    # trying; both are still above the 0.75 floor, so the band they leave --
    # 0.75 to the ceiling -- is where this profile does its work. Below the
    # floor the ceiling can never bind, so it does not touch the fallback.
    last_minute_max_price: float = 1.0
    # Below this many seconds the floor is dropped and the leader is bought
    # at whatever it costs.
    #
    # Not a relaxation of the rule -- the rest of it. The floor only fails to
    # clear while the two sides are still close, which is to say while the
    # round is genuinely undecided; and in exactly that case the dearer side
    # is both the best read available AND cheap, because "no side reached
    # 0.75" means the thing being bought is under 0.75 by definition. Holding
    # out past this point forfeits the round waiting for a price that the
    # book has already declined to print.
    last_minute_fallback_s: float = 45.0
    # Stop trying. A MARKET order needs a book to still be there, and the
    # last few seconds of a round are when it is not.
    last_minute_deadline_s: float = 5.0
    # Stake per round, as a fraction of bankroll. One side, one order, one
    # round -- so unlike a straddle leg this is the whole commitment to the
    # round rather than half of it.
    last_minute_stake_pct: float = 0.05

    # --- Scalping the futures lead ----------------------------------------
    # A fourth entry strategy, and the only one whose P&L does not depend on
    # the oracle at all. Spot follows the USD-M perpetual by milliseconds, and
    # the prediction market settles on spot -- so a perp move is a forecast of
    # the settlement quantity, and while the prediction book has not repriced
    # to it, the side the move points at is underpriced.
    #
    # That edge decays in about a second, so it is not held to settlement. The
    # position is bought, offered back out for a small fixed gain, and cut for
    # a small fixed loss if the move was wrong -- many round trips inside one
    # round rather than one all-or-nothing bet per round.
    #
    # See docs/superpowers/specs/2026-09-10-futures-lead-scalping-design.md.
    scalp: bool = False
    # Stake per ROUND TRIP, as a fraction of bankroll. Not per round: a round
    # may hold twenty of these, and each is opened only once the previous is
    # flat, so this is the exposure at any one instant.
    scalp_stake_pct: float = 0.05
    # Net gain and net loss per unit staked that close a position.
    #
    # These are P&L, not price moves, and the difference is the whole
    # economics of the profile. Selling n shares at q nets n*q*(1-f), so a
    # position filled at p needs q = p(1+r)/(1-f) to return r. At a 2% fee a
    # +5% RESULT needs a +7.14% price move while a -5% result arrives after
    # only -3.06%, which on a driftless walk is a 30% chance of the good
    # ending. bracket_prices() derives both from the market's own fee, and
    # signal_edge_required() turns that asymmetry into the one number worth
    # gating on.
    scalp_take_profit_pct: float = 0.05
    scalp_stop_loss_pct: float = 0.05
    # The window the perp move is measured over, and how far it must have
    # moved in it. 2 bps of BTC is roughly a 20-dollar move at 100k -- small
    # enough to happen many times a round, large enough not to be the spread
    # flickering.
    scalp_lookback_ms: float = 1500.0
    scalp_min_move_bps: float = 2.0
    # How far the perp/spot basis must have DISLOCATED from its own recent
    # mean, in bps, in the direction of the move. This is the "spot has not
    # caught up yet" half of the premise, and it is measured as a dislocation
    # rather than a level because the basis has a persistent non-zero mean --
    # funding, not information. Gating on the level would fire constantly on
    # one side and never on the other: a funding-rate detector wearing a
    # lead-lag costume. 0 disables the second condition.
    scalp_min_basis_bps: float = 0.5
    # A perp tick older than this is not a signal. The socket's own health
    # check cannot serve here: it asks whether ANY frame arrived recently,
    # across every subscribed symbol, and a busy BTC stream keeps it healthy
    # while a quiet altcoin's last tick is a minute old.
    scalp_max_tick_age_ms: float = 2000.0
    # Minimum gap between entries on one symbol. A signal that persists for
    # three passes is one signal, and without this it opens three positions.
    scalp_cooldown_s: float = 2.0
    # Hard ceiling per round. An unbounded re-entry loop is a way to pay the
    # venue fee twenty times a minute while believing it is a strategy.
    scalp_max_entries_per_round: int = 20
    # Cancel every scalp order and sell every scalp position at this many
    # seconds left. This is "stop in the last minute" -- the book thins there
    # and orders fail, so nothing is PLACED inside it either, which is why
    # entry_window_end_s must sit above this and not on it.
    scalp_flatten_s: float = 60.0
    # Refuse a market whose fee demands more signal than this.
    #
    # signal_edge_required() answers "how far above a no-information coin
    # flip must the hit rate be for this bracket to break even", in hit-rate
    # points. It is 0.00 at a zero fee and about 0.20 at 200 bps. Above this
    # threshold the bracket is a bet the arithmetic cannot defend, and the
    # market is skipped loudly rather than traded quietly.
    scalp_max_edge_required: float = 0.25

    # --- Claiming (background, non-blocking) --------------------------
    # A win must be claimed (on-chain redemption) before its proceeds are
    # real, spendable balance, and that can take anywhere from about a
    # second to roughly a minute. The trading loop must not sit still for
    # that: it keeps scanning for the next round's open immediately, while
    # a dedicated background worker retries the claim and polls its status
    # on this cadence, independent of poll_interval_s, until it lands.
    claim_poll_interval_s: float = 1.0
    claim_timeout_s: float = 90.0

    # --- Plumbing ----------------------------------------------------------
    # --- Timing (previously hardcoded inside the loop) ------------------
    clock_resync_s: float = 300.0        # re-sync the server clock this often
    settle_grace_s: float = 2.0          # wait after end before settling
    settle_timeout_s: float = 600.0      # give up settling and log the gap
    drain_timeout_s: float = 600.0       # wait for an open position on exit
    drain_poll_s: float = 5.0
    prune_after_s: float = 3600.0        # forget rounds this long past expiry
    vol_cache_s: float = 60.0
    error_backoff_max_s: float = 30.0
    # How long --preflight waits for a signed request to be ACCEPTED before
    # it gives up, and how often it retries while waiting.
    #
    # On shared egress the outbound address is not known until the process is
    # running and can change on any restart, so the address that has to be in
    # Binance's allowlist cannot be added in advance. Without this, preflight
    # fails in the first second of boot, the deploy dies, and the address is
    # gone before it can be pasted anywhere. Waiting turns that race into a
    # window: preflight prints the address, then keeps knocking until the
    # allowlist entry lands.
    #
    # Bounded rather than infinite on purpose -- an unbounded wait is a paid
    # worker sitting idle forever on a key that may simply be wrong. 0
    # disables the wait and fails on the first refusal.
    auth_wait_timeout_s: float = 0.0
    auth_wait_poll_s: float = 5.0

    # --- Tolerances and paging (previously hardcoded) -------------------
    round_duration_tolerance: float = 0.10    # fraction of the target length
    quote_consistency_tolerance: float = 0.10
    market_list_limit: int = 50
    # 50 could silently miss an older settlement and leave a position
    # looking unresolved when the venue had already settled it.
    settled_history_limit: int = 100

    db_path: str = "btc5m_journal.db"
    # Print the calibration report to the log every N settled trades. On a
    # hosted worker the journal sits on a disk you cannot easily read, so
    # without this the only record is one you have to go and fetch.
    report_every: int = 0
    poll_interval_s: float = 2.0
    recv_window_ms: int = 5000
    http_timeout_s: float = 10.0
    # -- WebSocket feeds ----------------------------------------------------
    # False drops the bot to REST-only, which is exactly the behaviour it had
    # before these existed. Resolves through ConfigStore, so it is the
    # rollback path: edit the file, and the running bot stops using the
    # sockets on its next reload without a redeploy.
    ws_enabled: bool = True
    ws_spot_url: str = "wss://stream.binance.com:9443/stream"
    ws_book_url: str = "wss://api.binance.com/sapi/wss"
    # USD-M perpetuals, a different host from spot. Read only by the scalp
    # strategy, and only over the socket: see FuturesFeed on why this one
    # feed has no REST fallback.
    ws_futures_url: str = "wss://fstream.binance.com/stream"
    # Silence on a socket for longer than this marks the feed unhealthy and
    # sends every read back to REST. Measured on the CONNECTION, never per
    # market: a book that is not changing sends nothing, so per-market
    # silence cannot tell a quiet market from a dead socket.
    ws_stale_s: float = 5.0
    ws_reconnect_max_s: float = 30.0
    # The venue closes the connection at 24h. Handing over early turns a
    # scheduled surprise into a planned one.
    ws_recycle_s: float = 82800.0        # 23h
    paper_start_bankroll: float = 100.0
    endpoints: tuple[tuple[str, str], ...] = ()
    profile_name: str = "custom"

    def __post_init__(self) -> None:
        if not self.api_key or not self.api_secret:
            raise ValueError("API key and secret are required")
        if not 0 < self.kelly_fraction <= 1:
            raise ValueError("kelly_fraction must be in (0, 1]")
        if not 0 < self.max_stake_pct <= 0.25:
            raise ValueError("max_stake_pct must be in (0, 0.25]")
        if self.hard_stake_multiple < 1.0:
            raise ValueError("hard_stake_multiple must be >= 1.0")
        if not 0 < self.hard_stake_ceiling <= 0.5:
            raise ValueError("hard_stake_ceiling must be in (0, 0.5]")
        if not 0 < self.min_edge < 1:
            raise ValueError("min_edge must be in (0, 1)")
        if self.min_edge_ratio < 0:
            raise ValueError("min_edge_ratio must be non-negative")
        if self.min_buffer_sigmas < 0:
            raise ValueError("min_buffer_sigmas must be non-negative")
        if not 0 < self.scale_in_initial_pct <= 1.0:
            raise ValueError("scale_in_initial_pct must be in (0, 1]")
        if not 0 < self.max_blended_price < 1.0:
            raise ValueError("max_blended_price must be in (0, 1)")
        if not (self.min_entry_price <= self.max_blended_price
                <= self.max_entry_price):
            # Above the band it can never bind; below it, no position could
            # ever satisfy it and top-ups would never happen. Either way the
            # setting silently does nothing, which is worse than an error.
            raise ValueError(
                f"max_blended_price {self.max_blended_price} must lie within "
                f"the entry band {self.min_entry_price}-{self.max_entry_price}")
        if self.min_win_return < 0:
            raise ValueError("min_win_return must be non-negative")
        if self.min_win_return > 0:
            # A floor the band can never satisfy is a bot that never trades
            # and never says why, which is the worst of the three outcomes.
            cap = max_price_for_return(self.min_win_return, self.fee_bps)
            if cap <= self.min_entry_price:
                raise ValueError(
                    f"min_win_return {self.min_win_return} caps the fill "
                    f"price at {cap:.4f} at {self.fee_bps} bps, which is at "
                    f"or below min_entry_price {self.min_entry_price}: no "
                    f"price could ever satisfy both")
        if not 0 < self.assumed_spread_pct < 1.0:
            raise ValueError("assumed_spread_pct must be in (0, 1)")
        if self.entry_order_type not in ("MARKET", "LIMIT"):
            raise ValueError(
                f"entry_order_type must be MARKET or LIMIT, got "
                f"{self.entry_order_type!r}")
        if self.exit_order_type not in ("NONE", "MARKET", "LIMIT", "BRACKET"):
            raise ValueError(
                f"exit_order_type must be NONE, MARKET, LIMIT or BRACKET, "
                f"got {self.exit_order_type!r}")
        if self.exit_trigger not in ("RESTING", "POLLED"):
            raise ValueError(
                f"exit_trigger must be RESTING or POLLED, got "
                f"{self.exit_trigger!r}")
        if self.exit_order_type == "MARKET" and self.exit_trigger == "RESTING":
            # A market order cannot rest on the book, so this pairing asks
            # for something the venue will not do. Coercing it to POLLED
            # would mean the config says one thing and the bot does another,
            # which is the failure that is found months later in a journal.
            raise ValueError(
                "exit_trigger RESTING requires exit_order_type LIMIT: a "
                "MARKET order cannot rest on the book")
        # Empty is valid: it means "no restriction, discover every market".
        if len(set(self.symbols)) != len(self.symbols):
            raise ValueError("symbols must not contain duplicates")
        if self.max_concurrent_positions < 1:
            raise ValueError("max_concurrent_positions must be >= 1")
        if not 0 <= self.reserve_pct < 1:
            raise ValueError("reserve_pct must be in [0, 1)")
        if self.decision_log_interval_s < 0:
            raise ValueError("decision_log_interval_s must be non-negative")
        if not 0 < self.round_duration_tolerance < 1.0:
            raise ValueError("round_duration_tolerance must be in (0, 1)")
        if self.settled_history_limit < 1:
            raise ValueError("settled_history_limit must be positive")
        if self.fill_confirm_attempts < 1:
            raise ValueError("fill_confirm_attempts must be >= 1")
        if not 0 < self.min_fill_fraction <= 1.0:
            raise ValueError("min_fill_fraction must be in (0, 1]")
        if self.fill_confirm_delay_s <= 0:
            raise ValueError("fill_confirm_delay_s must be positive")
        for name in ("clock_resync_s", "settle_grace_s", "settle_timeout_s",
                     "drain_timeout_s", "drain_poll_s", "prune_after_s",
                     "vol_cache_s", "error_backoff_max_s", "auth_wait_poll_s",
                     "ws_stale_s", "ws_reconnect_max_s", "ws_recycle_s"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        if self.ws_recycle_s >= 24 * 3600:
            raise ValueError(
                "ws_recycle_s must be under 24h: the venue closes the "
                "connection at 24h, and recycling at or after that point "
                "guarantees the drop arrives as an unplanned gap")
        if self.auth_wait_timeout_s < 0:
            raise ValueError("auth_wait_timeout_s must be non-negative")
        if self.trend_min_run < 1:
            raise ValueError("trend_min_run must be >= 1")
        if self.trend_max_run < self.trend_min_run:
            raise ValueError("trend_max_run must be >= trend_min_run")
        if self.trend_min_z < 0:
            raise ValueError("trend_min_z must be non-negative")
        if self.trend_min_impulse <= 0:
            raise ValueError("trend_min_impulse must be positive")
        if not 0 < self.trend_decay_floor <= 1.0:
            # Above 1.0 it would demand acceleration on every block, which
            # no real move sustains; at or below 0 it could never fire.
            raise ValueError("trend_decay_floor must be in (0, 1]")
        if self.trend_min_rounds_left < 0:
            raise ValueError("trend_min_rounds_left must be non-negative")
        if not 0 <= self.trend_min_efficiency <= 1:
            raise ValueError("trend_min_efficiency must be in [0, 1]")
        if self.trend_stake_multiple < 1.0:
            # Below 1.0 the "boost" would shrink the stake on exactly the
            # setups the profile is built to press, which is not a tuning
            # choice but a sign inversion.
            raise ValueError("trend_stake_multiple must be >= 1.0")
        if self.trend_early_entry_s < 0:
            raise ValueError("trend_early_entry_s must be non-negative")
        if self.trend_follow:
            # The run is counted in round-length blocks, so the window has to
            # hold at least trend_max_run of them or the ceiling can never be
            # reached and the fade rule silently never fires.
            needed = self.trend_max_run * self.round_seconds / 60.0
            if self.trend_lookback_min < needed:
                raise ValueError(
                    f"trend_lookback_min {self.trend_lookback_min} is shorter "
                    f"than trend_max_run x round_seconds ({needed:.0f} min); "
                    f"the run ceiling could never be reached")
        if self.report_every < 0:
            raise ValueError("report_every must be non-negative")
        if self.use_fat_tails and self.tail_df_floor <= 2.0:
            raise ValueError("tail_df_floor must exceed 2 for finite variance")
        if self.calibration_z_halt >= 0:
            raise ValueError("calibration_z_halt must be negative")
        if self.sigma_window_min > self.vol_lookback_min:
            raise ValueError("sigma_window_min must not exceed vol_lookback_min")
        if self.entry_window_end_s >= self.entry_window_start_s:
            raise ValueError("entry_window_end_s must be < entry_window_start_s")
        if not 0 < self.min_entry_price < self.max_entry_price < 1:
            raise ValueError("require 0 < min < max < 1 entry price")
        if self.vol_floor_annual <= 0:
            raise ValueError("vol_floor_annual must be positive")
        if not 0 <= self.fee_bps < 10_000:
            raise ValueError("fee_bps must be in [0, 10000)")
        if not 1 <= self.max_slippage_bps <= 10_000:
            raise ValueError("max_slippage_bps must be in [1, 10000]")
        if self.round_seconds <= 0:
            raise ValueError("round_seconds must be positive")
        if self.min_stake_usdt < 0.5:
            raise ValueError("min_stake_usdt below 0.50 is not plausible")
        if self.account_type not in ("AUTO", "SPOT", "FUNDING"):
            raise ValueError("account_type must be AUTO, SPOT or FUNDING")
        if self.funding_source not in ("AUTO", "MPC", "CEX"):
            raise ValueError("funding_source must be AUTO, MPC or CEX")
        if not 0 < self.straddle_stake_pct <= 0.25:
            raise ValueError("straddle_stake_pct must be in (0, 0.25]")
        if self.straddle_entry_window_s <= 0:
            raise ValueError("straddle_entry_window_s must be positive")
        if not 0 < self.straddle_max_leg_price <= 1.0:
            raise ValueError("straddle_max_leg_price must be in (0, 1]")
        if not 0 < self.straddle_first_leg_max_price < 0.5:
            # At 0.5 the first leg's payout only just covers an equal-sized
            # pair, leaving no room at all for the second leg to be worth
            # buying -- the strategy needs the first fill to be genuinely
            # cheap, not merely the better half of a coin flip.
            raise ValueError(
                "straddle_first_leg_max_price must be in (0, 0.5)")
        if self.straddle_hedge_deadline_s < 0:
            raise ValueError("straddle_hedge_deadline_s must be "
                             "non-negative")
        if self.straddle_min_worst_case_return < 0:
            raise ValueError(
                "straddle_min_worst_case_return must be non-negative")
        if self.straddle and self.scale_in:
            # Scale-in tops a position up toward the Kelly stake for a
            # rising model probability. The straddle strategy has no model
            # probability -- there is nothing for scale-in to top up toward.
            raise ValueError(
                "straddle and scale_in cannot both be enabled")
        if not 0 < self.last_minute_stake_pct <= 0.25:
            raise ValueError("last_minute_stake_pct must be in (0, 0.25]")
        if not self.last_minute_price_floor < self.last_minute_max_price <= 1.0:
            # At or below the floor no price could satisfy both, so the
            # primary branch could never fire and the profile would quietly
            # become fallback-only -- a different strategy wearing this
            # one's name. Above 1.0 it is not a price.
            raise ValueError(
                f"last_minute_max_price ({self.last_minute_max_price}) must "
                f"be above last_minute_price_floor "
                f"({self.last_minute_price_floor}) and at most 1.0")
        if not 0.5 < self.last_minute_price_floor < 1.0:
            # See the field comment: at or below 0.5 the floor can never
            # fail, so the fallback can never fire.
            raise ValueError("last_minute_price_floor must be in (0.5, 1)")
        if self.last_minute_deadline_s < 0:
            raise ValueError("last_minute_deadline_s must be non-negative")
        if not (self.last_minute_deadline_s < self.last_minute_fallback_s
                <= self.last_minute_start_s):
            # Ordered, or one of the three silently does nothing: a fallback
            # later than the start never gets a chance to hold the floor up,
            # and one earlier than the deadline never gets a chance to drop
            # it. Both read as configuration and behave as nothing.
            raise ValueError(
                f"require last_minute_deadline_s "
                f"({self.last_minute_deadline_s}) < last_minute_fallback_s "
                f"({self.last_minute_fallback_s}) <= last_minute_start_s "
                f"({self.last_minute_start_s})")
        if self.last_minute and self.scale_in:
            # Scale-in tops a position up toward the Kelly stake for a RISING
            # model probability. This strategy has no model probability --
            # there is nothing for a top-up to aim at.
            raise ValueError(
                "last_minute and scale_in cannot both be enabled")
        if not 0 < self.scalp_stake_pct <= 0.25:
            raise ValueError("scalp_stake_pct must be in (0, 0.25]")
        if not 0 < self.scalp_take_profit_pct < 1.0:
            raise ValueError("scalp_take_profit_pct must be in (0, 1)")
        if not 0 < self.scalp_stop_loss_pct < 1.0:
            # At or above 1.0 the stop price is negative and the position can
            # never be cut -- a "capped" loss that is the whole stake.
            raise ValueError("scalp_stop_loss_pct must be in (0, 1)")
        if self.scalp_lookback_ms <= 0:
            raise ValueError("scalp_lookback_ms must be positive")
        if self.scalp_min_move_bps <= 0:
            # At zero every tick is a signal in whichever direction the last
            # frame happened to land, which is a coin flip paying two fees.
            raise ValueError("scalp_min_move_bps must be positive")
        if self.scalp_min_basis_bps < 0:
            raise ValueError("scalp_min_basis_bps must be non-negative")
        if self.scalp_max_tick_age_ms <= 0:
            raise ValueError("scalp_max_tick_age_ms must be positive")
        if self.scalp_cooldown_s < 0:
            raise ValueError("scalp_cooldown_s must be non-negative")
        if self.scalp_max_entries_per_round < 1:
            raise ValueError("scalp_max_entries_per_round must be >= 1")
        if self.scalp_flatten_s < 0:
            raise ValueError("scalp_flatten_s must be non-negative")
        if not 0 <= self.scalp_max_edge_required < 0.5:
            # At or above 0.5 the gate can never bind: no fee makes the
            # required edge exceed half, so it would read as a safety limit
            # and behave as nothing.
            raise ValueError("scalp_max_edge_required must be in [0, 0.5)")
        if self.scalp:
            if self.entry_window_end_s <= self.scalp_flatten_s:
                # A scalp opened at the entry deadline is flattened the same
                # instant, so the profile would pay two fees for a position
                # it never held. The gap between the two is the runway a
                # fresh bracket gets.
                raise ValueError(
                    f"entry_window_end_s ({self.entry_window_end_s}) must be "
                    f"above scalp_flatten_s ({self.scalp_flatten_s}): a scalp "
                    f"opened at the deadline is closed before it can move")
            if self.scalp_max_tick_age_ms < self.scalp_lookback_ms:
                # A tick fresh enough to trade on must at least be able to
                # span the window the move is measured over, or every signal
                # is rejected by one gate or unmeasurable by the other.
                raise ValueError(
                    f"scalp_max_tick_age_ms ({self.scalp_max_tick_age_ms}) "
                    f"must be at least scalp_lookback_ms "
                    f"({self.scalp_lookback_ms})")
        # BRACKET and scalp are one decision wearing two names, so neither is
        # allowed without the other. A BRACKET exit with no scalp entry is an
        # exit mechanism nothing arms; a scalp with any other exit setting
        # would run the model-priced offer against a position the model has
        # no opinion about.
        if self.scalp != (self.exit_order_type == "BRACKET"):
            raise ValueError(
                "scalp requires exit_order_type BRACKET, and BRACKET requires "
                f"scalp (scalp={self.scalp}, "
                f"exit_order_type={self.exit_order_type!r})")
        # Two entry strategies behind one dispatch. Enabling any pair would
        # silently run whichever branch happens to be tested first.
        chosen = [n for n, on in (("straddle", self.straddle),
                                  ("last_minute", self.last_minute),
                                  ("scalp", self.scalp)) if on]
        if len(chosen) > 1:
            raise ValueError(
                f"only one entry strategy may be enabled, got: "
                f"{', '.join(chosen)}")
        if self.scalp and self.scale_in:
            # Scale-in tops a position up toward the Kelly stake for a rising
            # model probability. This strategy has no model probability, and
            # a top-up would move the fill price the whole bracket is
            # computed from after the bracket was already placed.
            raise ValueError("scalp and scale_in cannot both be enabled")
        if self.claim_poll_interval_s <= 0:
            raise ValueError("claim_poll_interval_s must be positive")
        if self.claim_timeout_s <= 0:
            raise ValueError("claim_timeout_s must be positive")

    @property
    def symbol(self) -> str:
        """
        A representative single market.

        Used only for informational purposes where exactly one symbol is
        needed -- preflight's spot/volatility probe, and PredictionClient's
        fallback when a venue settlement feed cannot be resolved to a
        Binance ticker. Does NOT restrict what gets traded; that is `symbols`
        (or its absence, which means "every market").
        """
        return self.symbols[0] if self.symbols else "BTCUSDT"

    @property
    def hard_max_stake_pct(self) -> float:
        """
        Ceiling for the venue-minimum override. Never below max_stake_pct.
        """
        return min(max(self.max_stake_pct * self.hard_stake_multiple,
                       self.max_stake_pct), self.hard_stake_ceiling)

    def ep(self, name: str) -> tuple[str, str]:
        """(method, path) for a named endpoint."""
        return (dict(self.endpoints) or DEFAULT_ENDPOINTS)[name]

    def ws_url(self, which: str) -> str:
        """
        Socket URL by feed name. Mirrors `ep` deliberately.

        Named lookup rather than two attribute reads for the same reason the
        endpoint table is one: a typo becomes a KeyError here instead of a
        connection to whatever the misspelled attribute happened to hold.
        """
        return {"spot": self.ws_spot_url, "book": self.ws_book_url}[which]
