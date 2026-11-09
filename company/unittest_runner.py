"""Fixed in-container runner; its reported counts are untrusted observational data."""
import json
import sys
import unittest

suite=unittest.defaultTestLoader.discover('/workspace',pattern='test_*.py',top_level_dir='/workspace')
discovered=suite.countTestCases()
print('Discovered tests:',discovered)
if discovered == 0:
    sys.exit(2)
result=unittest.TextTestRunner(verbosity=2).run(suite)
counts=dict(discovered=discovered,run=result.testsRun,failed=len(result.failures),errors=len(result.errors),
            skipped=len(result.skipped),expected_failures=len(result.expectedFailures),
            unexpected_successes=len(result.unexpectedSuccesses))
counts['passed']=max(0,result.testsRun-sum(counts[k] for k in ('failed','errors','skipped','expected_failures','unexpected_successes')))
print('AAC_RESULT_V1:'+json.dumps({'kind':'unit','counts':counts},sort_keys=True),file=sys.stderr)
sys.exit(not result.wasSuccessful())
