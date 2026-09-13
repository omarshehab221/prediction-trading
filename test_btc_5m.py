#!/usr/bin/env python3
"""
Test suite for btc_5m_predictor.

Covers the pure logic end to end, plus a full simulated trading session driven
by a fake client, so the loop, sizing, settlement and risk limits are exercised
without touching the network.

    python3 -m unittest test_btc_5m -v
"""

from __future__ import annotations

import unittest

# Every subject's tests, imported by name so `python -m unittest
# test_btc_5m` still collects the whole suite and `test_btc_5m.TestKelly`
# still names one class. The tests themselves live in tests/.
from tests.test_venue import (
    TestAuthWait,
    TestBalanceLookup,
    TestBatchRedeemResponses,
    TestEndpointMethods,
    TestErrorClassification,
    TestErrorSurfacing,
    TestFundingSourceDerivation,
    TestHostilePayloads,
    TestLimitQuoting,
    TestMinimumDiscovery,
    TestOrderPlan,
    TestOrderResponseHandling,
    TestOrderStateAndCancel,
    TestParseAsks,
    TestParseBids,
    TestParseRound,
    TestPerMarketFee,
    TestPredictionWalletBalance,
    TestQuoteErrorClassification,
    TestQuoteValidation,
    TestRedemption,
    TestRequestSigning,
    TestStrictFieldParsing,
    TestSymbolMapping,
    TestVariantParsing,
    TestVenueDerivedParameters,
)
from tests.test_pricing import (
    TestBracketArithmetic,
    TestBreakeven,
    TestDigitalPricing,
    TestEdgeThresholds,
    TestKelly,
    TestProjectedRounds,
    TestReservationPrice,
    TestSettlePnl,
    TestSignalEdgeRequired,
    TestSmallAccountSizing,
    TestStraddleCompletion,
    TestStraddleSplit,
    TestStudentT,
    TestTrendArithmetic,
    TestWalkBook,
    TestWeiUnits,
    TestWinReturn,
)
from tests.test_config import (
    TestBlendCapIsPerProfile,
    TestConfigFile,
    TestConfigValidation,
    TestConvexProfile,
    TestFavoriteProfile,
    TestHostingReadiness,
    TestHotReload,
    TestLastMinuteConfig,
    TestLimitConfig,
    TestMicroProfile,
    TestNoBakedInValues,
    TestProfileDefaults,
    TestProfileRiskCoherence,
    TestScalpConfig,
    TestScalpProfile,
    TestStraddleConfig,
    TestWsConfig,
)
from tests.test_risk import (
    TestBankrollViability,
    TestCalibrationBreaker,
    TestClampedSigmaGuard,
    TestRawSigmaDiagnostic,
    TestRiskManager,
    TestTailEstimation,
    TestTrendDetection,
    TestVolatilityCache,
    TestVolatilityReadsMarketData,
)
from tests.test_journal import (
    TestBiasReport,
    TestBufferReport,
    TestDiagnose,
    TestJournal,
    TestPerMarketFeeInDiagnostics,
    TestPerProfileReport,
    TestPeriodicReport,
)
from tests.test_assessment import (
    TestBlendedPriceCeiling,
    TestBoundaryConditions,
    TestBufferGate,
    TestEvaluate,
    TestGatesSeeThePricePaid,
    TestNewLimitsActuallyBind,
    TestOnlyBufferScalesIn,
    TestReturnFloor,
)
from tests.test_trader import (
    TestBalanceReconciliation,
    TestCancelRacesFill,
    TestLimitExits,
    TestLiveQuoteGate,
    TestLoopSurvivesUnexpectedFailures,
    TestMissedRoundReporting,
    TestModeSwitching,
    TestMultiMarket,
    TestPaperRestingOrders,
    TestPartialFills,
    TestPendingOrderLifecycle,
    TestScaleIn,
    TestScaleInSizing,
    TestSimulatedSession,
    TestSoldPositionsReachTheRiskManager,
    TestTieSettlement,
    TestTrendBoost,
)
from tests.test_strategies import (
    TestLastMinuteEntry,
    TestScalpEntry,
    TestScalpFlatten,
    TestScalpSignal,
    TestScalpStops,
    TestStraddleCompletionBar,
    TestStraddleEntry,
)
from tests.test_ws_feeds import (
    TestBookFeed,
    TestFuturesFeed,
    TestMarketData,
    TestScriptModeSharesOneSide,
    TestSpotFeed,
    TestWsConnection,
    TestWsRecycle,
)
from tests.test_cli import (
    TestMainEntryPoint,
    TestNewCliSurface,
    TestPreflightGate,
    TestSymbolsArgParsing,
    TestSymbolsEnv,
    TestTradingModeEnv,
)
from tests.test_source_rules import (
    TestCoherenceCorpus,
    TestNoRawTracebacks,
    TestNoSilentFailures,
    TestPropertyInvariants,
    TestSchemaConformance,
)
from tests.test_deployment import (
    TestDefaultProfileCoherence,
    TestDeploymentEntrypoint,
    TestDeploymentManifests,
    TestModeIsNotHardcoded,
    TestVerificationGate,
)


if __name__ == "__main__":
    unittest.main(verbosity=2)
