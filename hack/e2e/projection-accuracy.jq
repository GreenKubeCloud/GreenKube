# v0.4 exit criterion: the projected and measured allocated cost of a
# rightsizing change agree within the tolerance (roadmap §7, v0.4.0).
#
# Input: {"recommendation_ids": [ids], "tolerance": 0.2,
#         "changes": [GET /api/v2/changes/{id}/measurements responses]}
# Output: {"status": "ok" | "failed" | "warning", "reason", "checks": [...]}
#
# For every recommendation, the latest decided measurement that covers it is
# read: its allocated cost estimate (recommendations[].estimates, or the
# change-level cost estimate when the change has a single recommendation)
# gives measured = annualized and projected = projected_annual, and
#   ratio = |measured - projected| / |projected|.
# - ratio above the tolerance fails the check;
# - an inconclusive or contaminated measurement (the verifier could not decide,
#   by design: weak load driver, confounders, short window) or a missing
#   projection is a warning, never a pass;
# - a recommendation without any decided measurement fails.

def num: if type == "number" then . else null end;
def absolute: if . < 0 then -. else . end;

def cost_estimate($measurement; $rid):
  ([$measurement.recommendations[]? | select(.recommendation_id == $rid)] | last) as $row
  | (if $row != null then ([$row.estimates[]? | select(.quantity == "cost")] | first) else null end) as $allocated
  | if $allocated != null then {estimate: $allocated, outcome: ($row.outcome // $measurement.outcome), source: "allocated"}
    else
      ([$measurement.estimates[]? | select(.quantity == "cost")] | first) as $change
      | if $change != null and (($measurement.recommendations // []) | length) <= 1
        then {estimate: $change, outcome: $measurement.outcome, source: "change"}
        else null
        end
    end;

def check($changes; $rid; $tolerance):
  ([$changes[] | .change_id as $cid | .items[]? | . + {change_id: $cid}
     | select(([.recommendations[]?.recommendation_id] | index($rid)) != null
              or ((.recommendations // []) | length) == 0)]
   | sort_by(.decided_at) | last) as $m
  | if $m == null then
      {recommendation_id: $rid, status: "failed", reason: "no decided measurement"}
    else
      cost_estimate($m; $rid) as $c
      | if $c == null then
          {recommendation_id: $rid, change_id: $m.change_id, measurement_id: $m.measurement_id,
           status: "failed", reason: "measurement has no cost estimate"}
        else
          ($c.estimate.annualized | num) as $measured
          | ($c.estimate.projected_annual | num) as $projected
          | ($c.estimate.outcome // $c.outcome) as $outcome
          | {recommendation_id: $rid, change_id: $m.change_id, measurement_id: $m.measurement_id,
             source: $c.source, outcome: $outcome, measured_allocated: $measured,
             projected_allocated: $projected, tolerance: $tolerance}
          | if ($outcome == "inconclusive" or $outcome == "contaminated") then
              . + {ratio: null, status: "warning", reason: "measurement \($outcome) by design"}
            elif $projected == null or $projected == 0 then
              . + {ratio: null, status: "warning", reason: "no projected cost to compare"}
            elif $measured == null then
              . + {ratio: null, status: "failed", reason: "no measured cost"}
            else
              ((($measured - $projected) | absolute) / ($projected | absolute)) as $ratio
              | . + {ratio: $ratio,
                     status: (if $ratio > $tolerance then "failed" else "ok" end),
                     reason: (if $ratio > $tolerance
                              then "measured and projected cost differ by more than \($tolerance * 100)%"
                              else "within tolerance" end)}
            end
        end
    end;

(.tolerance // 0.2) as $tolerance
| (.changes // []) as $changes
| [(.recommendation_ids // [])[] | check($changes; .; $tolerance)] as $checks
| {
    status: (if ($checks | length) == 0 then "failed"
             elif any($checks[]; .status == "failed") then "failed"
             elif any($checks[]; .status == "warning") then "warning"
             else "ok" end),
    reason: (if ($checks | length) == 0 then "no recommendation to check" else null end),
    checks: $checks
  }
