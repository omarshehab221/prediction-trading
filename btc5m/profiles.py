"""
The strategies, as named sets of settings. DEFAULT_PROFILE is the one
the deployment manifests seed on first boot.
"""

from __future__ import annotations

PROFILES: dict[str, dict] = {
    # Big wins, small losses. Buys cheap contracts, so it LOSES MOST ROUNDS by
    # construction; the winners have to be large enough to pay for them.
    "convex": {"max_entry_price": 0.35, "min_entry_price": 0.05,
                   "min_edge": 0.02, "min_edge_ratio": 0.30,
                   "max_stake_pct": 0.02, "entry_window_start_s": 280,
                   "entry_window_end_s": 30, "max_consecutive_losses": 60,
                   # Longshots: many small losses, so the default fits.
                   "daily_loss_limit_pct": 0.20, "assumed_spread_pct": 0.10,
                   # Tail probabilities are the least reliable part of the
                   # model, and this profile lives on them, so size more
                   # cautiously than a mid-price strategy would.
                   "kelly_fraction": 0.20,
                   "min_liquidity": 0.0, "max_rounds_per_day": 200,
                   "paper_start_bankroll": 100.0,
                   # No floor needed: the band's own top, 0.35, already pays
                   # about 180% on a win. Stated rather than inherited so a
                   # change to the default cannot silently reshape this.
                   "min_win_return": 0.0,
                   # Inert here unless scale_in is enabled; sized to this
                   # profile's own band (0.05-0.35), not buffer's.
                   "max_blended_price": 0.3},
    # Symmetric: trades anywhere it finds an edge. Higher hit rate, smaller
    # payoffs, and correspondingly larger individual losses.
    "balanced": {"max_entry_price": 0.90, "min_entry_price": 0.10,
                     "min_edge": 0.04, "min_edge_ratio": 0.10,
                     "max_stake_pct": 0.05, "entry_window_start_s": 150,
                     "entry_window_end_s": 25, "max_consecutive_losses": 10,
                     "daily_loss_limit_pct": 0.20, "assumed_spread_pct": 0.06,
                   "kelly_fraction": 0.25,
                     "min_liquidity": 0.0, "max_rounds_per_day": 200,
                     "paper_start_bankroll": 100.0,
                     # Symmetric by design: it trades wherever an edge is,
                     # including the expensive end, so a return floor would
                     # amputate half of what this profile is for.
                     "min_win_return": 0.0,
                     # Inert here unless scale_in is enabled; sized to this
                     # profile's own band (0.10-0.90), not buffer's.
                     "max_blended_price": 0.8},
    # For small accounts, where a percentage cap would fall under the venue's
    # order minimum and the bot would simply never trade. Targets the 0.40-0.75
    # band -- roughly 30-150% return per win. Those are large PERCENTAGE wins
    # that merely look small in dollars on a small balance.
    #
    # 20% of bankroll per round is deliberately aggressive and is the price of
    # trading a small account at all. It is survivable (a loss is bounded and
    # 20 consecutive losses still leave ~4% of the balance) but it is NOT the
    # Kelly-optimal fraction, and scaling stake up after wins compounds both
    # directions. Move to "balanced" once the balance clears ~30 USDT.
    # Buy the favourite, late in the round, while the return is still worth
    # having. Derived from a rule of "return >= 25%", i.e. price <= 0.80.
    #
    # This is the OPPOSITE side of the market from "convex". Prediction and
    # betting markets frequently show a favourite-longshot bias, in which
    # longshots are overpriced and favourites underpriced -- if that holds
    # here, this profile is on the right side of it and convex is on the
    # wrong one. `--calibration-report` measures which, from real fills.
    #
    # Entering late is a genuine information edge, not superstition: with
    # less time left, the same price move is far more decisive, so the
    # model's probability is sharper. The cost is a thinner book.
    "favorite": {"max_entry_price": 0.80, "min_entry_price": 0.55,
                     "min_edge": 0.03, "min_edge_ratio": 0.05,
                     "max_stake_pct": 0.10, "min_stake_usdt": 1.0,
                     "daily_loss_limit_pct": 0.30, "assumed_spread_pct": 0.04,
                   "kelly_fraction": 0.25,
                     "min_liquidity": 0.0, "max_rounds_per_day": 200,
                     "entry_window_start_s": 120, "entry_window_end_s": 20,
                     "max_consecutive_losses": 10, "paper_start_bankroll": 25.0,
                     # The 0.80 band top already implies ~25% at a 2% fee, so
                     # a floor here would only duplicate the band -- and at a
                     # market with a higher fee it would start rejecting
                     # trades this profile was built to take.
                     "min_win_return": 0.0,
                     # Inert here unless scale_in is enabled; sized to this
                     # profile's own band (0.55-0.80), not buffer's.
                     "max_blended_price": 0.72},
    # SCALP THE FUTURES LEAD. The only profile whose P&L never touches the
    # oracle.
    #
    # Spot follows the USD-M perpetual by milliseconds and the prediction
    # market settles on spot, so a perp move is a forecast of the settlement
    # quantity. While the prediction book has not repriced to it, the side
    # the move points at is underpriced. That edge decays in about a second,
    # so nothing here is held to settlement: buy, offer the shares back out
    # for +5% of stake, cut at -5%, and go again. Many small round trips in
    # one round instead of one all-or-nothing bet per round.
    #
    # WHAT THE FEE DOES TO THIS, STATED PLAINLY
    # -----------------------------------------
    # The bracket is symmetric in money and asymmetric in price, because the
    # fee is paid on the way out of both legs. At 200 bps a +5% RESULT needs
    # the token to rise 7.14% while a -5% result arrives after it falls only
    # 3.06% -- so a market with no opinion at all hits the stop 70% of the
    # time, and the signal must lift the hit rate 20 points above a coin flip
    # before the first unit of profit exists. At a market whose published
    # feeRateBps is near zero the same bracket is symmetric and "more right
    # than wrong" is exactly the bar.
    #
    # scalp_max_edge_required is that number made into a gate, so the profile
    # refuses a market whose arithmetic it cannot defend rather than trading
    # it quietly. It is the first thing to look at if this profile is losing.
    #
    # THE WINDOW IS TWO NUMBERS, NOT ONE
    # ----------------------------------
    # Entries stop at 75s and everything is flattened at 60s. The last minute
    # is when the book thins and orders fail, so nothing is PLACED inside it
    # -- which means the flatten has to happen at its edge, and entries have
    # to stop before that with enough runway for a fresh bracket to resolve.
    #
    # poll_interval_s is 1.0 rather than the usual 2.0. This is the shape of
    # the strategy, not the speed of it: the signal is read inside a poll
    # loop, so what is actually traded is a one-to-two-second momentum
    # signal, and the millisecond lead the premise describes is future work.
    "scalp": {"scalp": True,
              # Wide, because the band is not this profile's gate -- the
              # bracket is. What the band does exclude is the ends of the
              # book, where a 7% rise runs into 1.00 and bracket_prices
              # refuses the position anyway; stating it here means the round
              # is skipped before a quote is spent on it.
              "max_entry_price": 0.85, "min_entry_price": 0.15,
              # Inert: no model probability is computed on this path, so
              # nothing consults them. Declared at the defaults so a change
              # to the defaults cannot make them start mattering.
              "min_edge": 0.02, "min_edge_ratio": 0.30,
              "kelly_fraction": 0.25, "min_win_return": 0.0,
              # The stake that matters. max_stake_pct is the ceiling the
              # shared risk checks read; scalp_stake_pct is what is actually
              # committed per round trip, and they are kept equal so the two
              # cannot drift into disagreeing about the same number.
              "scalp_stake_pct": 0.05, "max_stake_pct": 0.05,
              "scalp_take_profit_pct": 0.05, "scalp_stop_loss_pct": 0.05,
              "scalp_lookback_ms": 1500.0, "scalp_min_move_bps": 2.0,
              "scalp_min_basis_bps": 0.5, "scalp_max_tick_age_ms": 2000.0,
              "scalp_cooldown_s": 2.0, "scalp_max_entries_per_round": 20,
              "scalp_flatten_s": 60.0, "scalp_max_edge_required": 0.25,
              "entry_window_start_s": 300, "entry_window_end_s": 75,
              "poll_interval_s": 1.0,
              # MARKET in, resting LIMIT out. The entry crosses because the
              # whole premise is being early to a move the book has not
              # priced yet, and a resting bid fills when someone wants to
              # sell into it -- which is precisely when the move is going the
              # other way. The take-profit rests for the opposite reason:
              # nothing about it is urgent, and resting earns the spread the
              # entry just paid.
              "entry_order_type": "MARKET",
              "exit_order_type": "BRACKET",
              # Inert under BRACKET: the exit price comes from the fill, not
              # from a model reading of the book. Declared so it cannot be
              # mistaken for a live setting on this profile.
              "exit_trigger": "RESTING",
              # A BRACKETED loss is 5% of the stake -- 0.25% of bankroll --
              # so 20% is eighty of them, which is the right order for a
              # strategy taking twenty trades a round. It is deliberately
              # not tightened below the shared 2.5x-of-max_stake_pct floor,
              # because the bracket is not the only way to lose here: a
              # flatten that fails leaves a position to settle, and that
              # loses the FULL stake. Four of those is what 20% buys, and
              # four failed flattens in a day is a venue problem worth
              # halting on.
              #
              # The streak counter is raised instead, since a run of small
              # bracketed losses is the ordinary texture of this profile and
              # 40 in a row is a broken signal rather than a bad afternoon.
              "daily_loss_limit_pct": 0.20,
              "max_consecutive_losses": 40,
              # rounds_today counts ROUND TRIPS here, not rounds, because
              # every scalp closes by selling and reports its result
              # individually. 20 per round x 12 rounds an hour is 240 an
              # hour, so a day's ceiling has to be in the thousands or this
              # halts before the first hour is out.
              "max_rounds_per_day": 4000,
              # A thin book is what breaks this strategy: the entry crosses
              # the spread and the exit needs a bid to sell into, and both
              # get worse faster than the price does. Stricter than every
              # other profile on purpose.
              "min_liquidity": 150.0, "max_price_impact": 0.02,
              "assumed_spread_pct": 0.03,
              # Never: a top-up would move the fill price the bracket was
              # already computed from. Config rejects the pairing; this is
              # the belt to that brace.
              "scale_in": False,
              # Inert with scale_in off, and sized to this profile's own
              # band so the shared band check passes on a real number.
              "max_blended_price": 0.80,
              "paper_start_bankroll": 100.0},
    # YOUR METHOD, encoded. Wait for a buffer to open up, back the side it
    # favours, press it while the market has inertia -- and refuse any price
    # whose win is too small to be worth the loss it risks.
    #
    # THE RETURN FLOOR IS WHAT SHAPES THIS PROFILE
    # --------------------------------------------
    # An earlier version traded 0.80-0.95. Those are genuine edges, but at
    # 0.94 a win pays about 6%, so ONE loss erases sixteen wins and a day of
    # patient work is undone by a single round going the other way. The edge
    # test cannot see this: edge is measured in probability and the problem
    # is measured in money.
    #
    # min_win_return=0.25 says a win must pay at least a quarter of the
    # stake. At a 2% fee that caps the fill at about 0.797, so the band ends
    # there and roughly four wins cover a loss instead of sixteen.
    #
    # WHAT THAT COSTS, STATED PLAINLY
    # -------------------------------
    # Price and buffer move together: a 1.5-sigma buffer is a ~93% chance and
    # a market that has noticed will quote near 0.93, which this profile now
    # refuses. So the trades that remain are the ones where the buffer is
    # real but the BOOK HAS NOT CAUGHT UP -- the venue still quoting 0.75
    # while spot has already moved. That is the manual edge being encoded,
    # and there are fewer such rounds than there were cheap-looking 0.94s.
    # Expect materially fewer trades and larger individual wins.
    #
    # min_buffer_sigmas is 0.75 for the same reason. Demanding 1.5 sigmas
    # while capping the price at 0.80 asks for a 93% chance at a 79% price,
    # which almost never coexists; 0.75 sigmas is a ~77% chance, so a venue
    # quote at or under 0.797 is a live disagreement rather than a fantasy.
    "buffer": {"max_entry_price": 0.80, "min_entry_price": 0.55,
                   "min_edge": 0.012, "min_edge_ratio": 0.010,
                   # A win must pay at least 25% of the stake, after fees.
                   # This, not max_entry_price, is the binding ceiling: it
                   # tracks each market's own published fee instead of
                   # assuming one.
                   "min_win_return": 0.25,
                   # See the note above: the price cap and the buffer gate
                   # pull against each other, and 0.75 is where both can be
                   # satisfied often enough to produce trades.
                   "min_buffer_sigmas": 0.75,
                   "max_stake_pct": 0.10, "min_stake_usdt": 1.0,
                   # A loss here costs a full 10% of bankroll, so the 20%
                   # default halted the day after TWO losses -- on 80% of
                   # days at an 85% win rate. The limit has to match the
                   # profile's own loss shape or it stops a healthy bot.
                   "daily_loss_limit_pct": 0.35,
                   # Thin books still destroy an edge measured in single
                   # points, but 0.02 was tuned for near-certainty fills at
                   # 0.90+. Inside the return floor's band the stakes are
                   # smaller relative to the book, so this was rejecting
                   # rounds whose price was fine. Loosened deliberately: it
                   # is a fill-quality gate, not a return gate, and bundling
                   # the two is what made the floor look like it was cutting
                   # trade count.
                   "max_price_impact": 0.05,
                   "assumed_spread_pct": 0.03,
                   "kelly_fraction": 0.25,
                   # 1000 USDT of resting depth is more than a 5-minute
                   # market typically shows, so this gate alone was capable
                   # of rejecting every round -- silently, and for a reason
                   # that has nothing to do with the price being good. It
                   # exists to avoid unfillable books, and 150 does that.
                   "min_liquidity": 150.0, "max_rounds_per_day": 250,
                   # A smaller first tranche leaves more room to add once the
                   # round has proven itself, so the top-up genuinely is the
                   # larger bet -- roughly 4x the opener rather than 1.5x.
                   # Total exposure is still bounded by Kelly, and by the
                   # blended-price cap below.
                   "scale_in": True, "scale_in_initial_pct": 0.25,
                   "scale_in_min_topup": 1.0,
                   # Inside the return floor's own ceiling on purpose. A
                   # top-up is bought at a HIGHER price than the opener, so
                   # the blend is the number that decides the payout, and
                   # letting it drift to 0.797 would spend the whole return
                   # budget on the last tranche.
                   "max_blended_price": 0.78,
                   # INERTIA, caught at its start rather than after the fact.
                   # trend_min_impulse is the trigger and does its work on
                   # the CURRENT block, so a move gets backed on its first
                   # thrust; trend_min_run=1 is what allows that. The run
                   # ceiling and the decay floor are the other half: a move
                   # that has already run five rounds, or that is shedding
                   # more than 45% of its size block over block, is bought at
                   # its worst price and is refused. trend_min_rounds_left=1
                   # asks the only question the entry actually poses -- does
                   # this survive the round I am entering?
                   "trend_follow": True,
                   "trend_min_impulse": 1.2, "trend_min_run": 1,
                   "trend_max_run": 5, "trend_decay_floor": 0.55,
                   "trend_min_rounds_left": 1.0, "trend_min_z": 0.8,
                   "trend_min_efficiency": 0.40,
                   "trend_stake_multiple": 1.5, "trend_early_entry_s": 90,
                   "trend_lookback_min": 30,
                   # Nearly the whole round. The return floor means the good
                   # price and the buffer rarely coexist for long, so the
                   # window has to be open when they do -- a narrow window
                   # turns "no trade was available" into "we were not
                   # looking", and those are not the same thing.
                   "entry_window_start_s": 270, "entry_window_end_s": 15,
                   "max_consecutive_losses": 6, "paper_start_bankroll": 25.0},
    "micro": {"max_entry_price": 0.75, "min_entry_price": 0.35,
                  "min_edge": 0.03, "min_edge_ratio": 0.06,
                  "max_stake_pct": 0.20, "min_stake_usdt": 1.0,
                  # 20% per trade means the 20% default halted after ONE loss.
                  # Even 45% halts after 2.25. At this stake fraction the
                  # stake cap and the daily limit are in genuine tension --
                  # that tension is forced by a 1.00 order minimum on a ~7
                  # balance, not chosen. Trading a bigger account is the only
                  # real fix; 55% is the least-bad compromise.
                  "daily_loss_limit_pct": 0.55,
                  "assumed_spread_pct": 0.05,
                  # A tiny balance forces a large stake fraction, so cap the
                  # hard ceiling tightly and let Kelly stay conservative.
                   "kelly_fraction": 0.25,
                  "min_liquidity": 0.0, "max_rounds_per_day": 200,
                  "entry_window_start_s": 200, "entry_window_end_s": 25,
                  "max_consecutive_losses": 10, "paper_start_bankroll": 7.0,
                  # A balance this small needs every trade it can get; a
                  # return floor on top of the band would leave it flat.
                  "min_win_return": 0.0,
                  # Inert here unless scale_in is enabled; sized to this
                  # profile's own band (0.35-0.75), not buffer's.
                  "max_blended_price": 0.65},
    # Buy BOTH sides in the first seconds of a round, but only when the two
    # payouts both beat what the pair costs -- see straddle_* on Config, and
    # _straddle_payouts_clear for the gate itself. 20% of bankroll per leg,
    # so ~40% committed per round. Deliberately large: at 2.5% a small
    # bankroll produced a per-leg stake under min_stake_usdt and the profile
    # silently never traded.
    "straddle": {"straddle": True, "straddle_stake_pct": 0.20,
                 # The first minute, and then four minutes to hedge in.
                 # Opening late is what strands legs; opening early is only
                 # possible at a price the early book offers, which is why
                 # this moves together with straddle_first_leg_max_price.
                 "straddle_entry_window_s": 60.0,
                 # No side is ever picked by price here, so these bands are
                 # left at their widest legal setting rather than inherited
                 # from another profile -- nothing below should silently
                 # reject a leg the straddle logic already screened.
                 "min_entry_price": 0.01, "max_entry_price": 0.99,
                 # Matched to straddle_stake_pct, not inherited. The straddle
                 # path never consults max_stake_pct -- it sizes off
                 # straddle_stake_pct directly -- so leaving this at 5% while
                 # each leg staked 20% made the config declare a cap the bot
                 # did not honour, and made --check-config and --preflight
                 # both report a limit that was never in force.
                 "max_stake_pct": 0.20, "min_edge": 0.02,
                 # 30% held back leaves 70% spendable, and one round needs
                 # 40% (two 20% legs). That capped the profile at a single
                 # live round no matter what max_concurrent_positions said.
                 # 10% leaves room for the second round the slot count is
                 # there for; a third is still refused for want of funds.
                 "reserve_pct": 0.10,
                 "min_edge_ratio": 0.0,
                 # A straddle's worst case per round is roughly one leg's
                 # stake (the other leg always pays something back), so 20%
                 # per leg means a bad round costs about 20% of bankroll. At
                 # the old 20% daily limit that halted the bot for the day
                 # after a SINGLE bad round; 50% leaves the 2.5-loss headroom
                 # every other profile has.
                 "daily_loss_limit_pct": 0.50,
                 "assumed_spread_pct": 0.10, "kelly_fraction": 0.25,
                 "min_liquidity": 0.0, "max_rounds_per_day": 400,
                 "paper_start_bankroll": 100.0, "min_win_return": 0.0,
                 "max_blended_price": 0.5,
                 # Never on by default for this profile: scale-in tops up
                 # toward a model probability this strategy does not have.
                 # Opt in explicitly (and only after reading why it is off)
                 # by overriding it back to True in your own config.
                 "scale_in": False,
                 # Two legs per round occupy two slots; four lets a second
                 # round's straddle open while the first is still settling.
                 "max_concurrent_positions": 4,
                 # THE gate. A round is entered only when both legs would pay
                 # back more than the pair cost together -- UP wins and the
                 # payout beats the whole stake, DOWN wins and it beats the
                 # whole stake -- so the round's direction stops mattering.
                 # That is a real condition, not a formality: it holds only
                 # while the two fee-adjusted prices sum to under 1.00, so
                 # most rounds are refused and the bot re-tests each one
                 # every poll until its window closes. Expect long stretches
                 # of no trades; --calibration-report and the periodic
                 # "no trade in N rounds" summary are how you tell that
                 # apart from a broken feed.
                 "straddle_require_positive_worst_case": True,
                 # 0.0 means "strictly more than the stake, by any margin".
                 # Raise it to demand a minimum locked-in return -- 0.01 for
                 # 1% of the pair, and correspondingly fewer rounds.
                 "straddle_min_worst_case_return": 0.0,
                 # Hold an unhedged leg to settlement rather than paying up
                 # for the other side at the deadline.
                 #
                 # The default is the opposite, and its reasoning is sound in
                 # general: at the deadline the guarantee is already gone, so
                 # the only question left is whether the position stays
                 # all-or-nothing, and a bounded loss beats a coin flip on
                 # the whole stake.
                 #
                 # It stops being sound at the price this profile now opens
                 # at. A leg bought at 0.40 can only be hedged near the
                 # deadline at something close to 0.60, which locks in a loss
                 # on nearly the entire pair -- paying most of the stake to
                 # convert a bet the book still prices near even money into a
                 # certain loss. The cheap-opener case that force hedging was
                 # written for (0.25 against a 0.75 hedge) is no longer the
                 # case this profile is usually in.
                 #
                 # So the leg rides. Straddle rounds that fail to complete
                 # become directional bets at roughly the odds they were
                 # opened at, which is the exposure the opening ceiling is
                 # chosen to bound. Watch the "only the UP leg opened"
                 # warnings: if they are common, the entry window and the
                 # ceiling are wrong, not this switch.
                 "straddle_force_hedge": False},
    # ONE RULE. With a minute left, buy whichever side is dearer -- the one
    # the book has already picked -- provided it is quoted at 0.75 or better.
    # Inside the last 45 seconds, buy it whatever it costs. Nothing else is
    # consulted and nothing else is done: no model probability, no edge test,
    # no buffer, no trend, no scale-in, no second leg.
    #
    # WHAT THE TWO HALVES COST, STATED PLAINLY
    # ----------------------------------------
    # They are not the same trade, and the expensive one is not the fallback.
    # A 0.75 fill pays about 33% on a win at a 2% fee, so 3 wins cover a
    # loss. But the floor has NO ceiling above it, and a round already
    # decided at 55 seconds quotes 0.97 -- which this profile buys, for about
    # 3% on a win, where it takes 32 wins to cover one loss. That is where
    # this strategy can bleed, and it is the primary branch, not the
    # fallback: the fallback only ever fires below 0.75 and therefore only
    # ever buys the CHEAP end.
    #
    # No max_entry_price is imposed to stop that, deliberately -- the band is
    # left at its widest legal setting so nothing downstream quietly
    # reinstates a filter this profile was written to do without. The
    # favourite-longshot table in --calibration-report is what says whether
    # the venue's late favourites win often enough to pay for the dear ones.
    "lastminute": {"last_minute": True, "last_minute_stake_pct": 0.10,
                   "last_minute_start_s": 60.0,
                   "last_minute_price_floor": 0.75,
                   # No ceiling, stated rather than inherited. This is the
                   # rule as asked for: above the floor, price is not a
                   # reason to refuse. Set it to 0.90 or 0.85 to stop buying
                   # rounds the book has already finished pricing.
                   "last_minute_max_price": 1.0,
                   "last_minute_fallback_s": 45.0,
                   "last_minute_deadline_s": 5.0,
                   # Matched to last_minute_stake_pct, not inherited. The
                   # last-minute path never consults max_stake_pct -- it
                   # sizes off its own fraction directly -- so a disagreeing
                   # value would make --check-config and --preflight both
                   # report a cap that is not in force, which is exactly the
                   # lie the straddle profile had to be corrected for.
                   "max_stake_pct": 0.10,
                   # Widest legal band. No side is ever chosen on a price
                   # judgement here beyond the floor itself, so nothing below
                   # should silently reject a leader the rule already picked.
                   "min_entry_price": 0.01, "max_entry_price": 0.99,
                   # Inert on this path -- there is no edge computation to
                   # threshold -- but stated rather than inherited so a
                   # change to the defaults cannot reshape this profile.
                   "min_edge": 0.02, "min_edge_ratio": 0.0,
                   "min_win_return": 0.0, "min_buffer_sigmas": 0.0,
                   # A loss costs a full 10% of bankroll, so the 20% default
                   # would halt the day after two of them. 35% leaves the
                   # 3.5-loss headroom the other single-sided profiles have.
                   "daily_loss_limit_pct": 0.35,
                   # Tight: entries happen in the last minute, when the two
                   # sides have separated and the leader's quote is firm.
                   "assumed_spread_pct": 0.03,
                   "kelly_fraction": 0.25,
                   # A late book is thinner than a mid-round one, and this
                   # profile has no way to wait for a better one, so a
                   # liquidity floor here would simply refuse rounds without
                   # improving the fills it does get. The venue's own FOK
                   # kill is the real protection; see _place_leg.
                   "min_liquidity": 0.0, "max_rounds_per_day": 300,
                   "paper_start_bankroll": 100.0,
                   # Inert unless scale_in is enabled, which validation
                   # forbids for this profile; sized to its own band.
                   "max_blended_price": 0.5,
                   # Never on: there is no model probability to top up
                   # toward, which is why Config rejects the combination.
                   "scale_in": False,
                   # One position per round and one round per market, so two
                   # slots is two live markets, not two bets on one.
                   "max_concurrent_positions": 2,
                   # 30% held back leaves 70% spendable against a 10% stake,
                   # which funds both slots with room to spare.
                   "reserve_pct": 0.30},
}


# The default strategy, declared once. Previously six literals across four
# files each carried their own copy of this, which is precisely how a default
# drifts: change five and the sixth silently disagrees.
DEFAULT_PROFILE = "lastminute"
