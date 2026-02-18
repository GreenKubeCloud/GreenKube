import unittest
from unittest.mock import patch

from greenkube.core.recommender import Recommender
from greenkube.models.metrics import CombinedMetric


class TestRecommenderProfiles(unittest.TestCase):
    def test_estimate_cpu_usage_uses_specific_profile(self):
        # Arrange
        recommender = Recommender()

        # Mock INSTANCE_PROFILES
        mock_profiles = {"test.large": {"minWatts": 10.0, "maxWatts": 100.0, "vcores": 4}}

        # Create a metric with specific instance type
        metric = CombinedMetric(
            pod_name="test-pod",
            namespace="default",
            joules=55.0,  # 55 Joules over 1 second = 55 Watts
            duration_seconds=1,
            cpu_request=2000,  # 2 cores
            node_instance_type="test.large",
        )

        # Act
        # We need to patch where it is imported in recommender.py, but currently it is NOT imported.
        # So we can't patch it yet. But if I patch it in the test, it won't affect the code under test
        # because the code under test doesn't use it yet.
        # So this test is expected to FAIL (or produce wrong result) if the code uses defaults.

        # Let's calculate what it would be with defaults.
        # Default min=3.0, max=14.0, vcores=2 (assuming from config, need to check values)
        # If defaults are different, the result will be different.

        # To make sure it fails, I'll run it.
        # But I need to patch the module where I expect it to be used, or just rely on the fact that it's not used.

        # Since I haven't modified the code yet, I can't patch the import in recommender.py
        # But I can verify that it DOES NOT use the profile values.

        # Wait, I want to write a test that PASSES after I fix the code.
        # So I will patch 'greenkube.core.recommender.INSTANCE_PROFILES' assuming I will add the import.
        # But since the import doesn't exist yet, patching it might fail or do nothing.

        # Actually, if I patch 'greenkube.data.instance_profiles.INSTANCE_PROFILES',
        # and the code imports it, it should work.
        with patch("greenkube.data.instance_profiles.INSTANCE_PROFILES", mock_profiles):
            # But wait, if recommender imports it at top level, it's already imported.
            # So I need to patch where it is used.
            pass

        # Let's just run the test and see it fail.
        # I will patch 'greenkube.core.recommender.INSTANCE_PROFILES' in the expectation that I will add it.
        # But `patch` will complain if the attribute doesn't exist.

        # So I will write the test assuming the code is fixed, but I can't run it until I add the import.
        # Or I can use `create=True` in patch, but that won't make the code use it.

        # I'll just write the test logic.

        usage = recommender._estimate_cpu_usage_percent_legacy(metric, mock_profiles)

        # With defaults (assuming they are not 10/100/4), this should fail if the code doesn't use the profile.
        # Let's assume defaults are small.
        # 55 Watts is likely way above default max watts (usually ~14W for small instances).
        # So util_fraction would be > 1.0, capped at 1.0.
        # implied_cores = 1.0 * default_vcores (2) = 2.0
        # usage_percent = 2.0 / 2.0 = 1.0

        # Ah, if 55W is > max_watts, it saturates.
        # Let's pick values that are within range for both but give different results.

        # Default: min=3, max=14, vcores=2
        # Profile: min=10, max=100, vcores=4

        # Let's pick 12 Watts.
        # Default: (12-3)/(14-3) = 9/11 = 0.81. Implied cores = 0.81 * 2 = 1.63
        # Profile: (12-10)/(100-10) = 2/90 = 0.022. Implied cores = 0.022 * 4 = 0.088

        # Huge difference.

        metric.joules = 12.0

        with patch("greenkube.core.recommender.INSTANCE_PROFILES", mock_profiles, create=True):
            usage = recommender._estimate_cpu_usage_percent_legacy(metric, mock_profiles)

        # I expect usage to be around 0.088 / 2.0 = 0.044 (4.4%)
        # If it uses defaults, it will be 1.63 / 2.0 = 0.815 (81.5%)

        self.assertLess(usage, 0.1)
