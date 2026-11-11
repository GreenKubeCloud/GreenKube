# Pick the pull request an interrupted e2e run (E2E_FRESH=0) resumes from.
#
# Input: {"prs": [PR records], "plans": {plan_id: plan status}, "now": epoch seconds,
#         "max_age": seconds}
# Output: the newest resumable PR record, or nothing.
#
# - An open PR is resumed from the merge step.
# - A merged PR is resumed from the controller sync only while its plan has not
#   reached "applied" (merged or synced) and it was merged at most max_age ago.
#   A merged PR whose plan is applied belongs to a loop that already went
#   through apply detection (an earlier run): the run starts a new loop instead.

def epoch:
  if . == null then null
  else sub("\\.[0-9]+"; "") | sub("(\\+00:00|\\+0000)$"; "Z") | (try fromdateiso8601 catch null)
  end;

. as $in
| [
    $in.prs[]
    | select(.plan_id != null)
    | select(
        .status == "open"
        or (
          .status == "merged"
          and (($in.plans[.plan_id] // "") as $plan | $plan == "merged" or $plan == "synced")
          and (((.merged_at // .updated_at // .created_at) | epoch) as $at | $at != null and ($in.now - $at) <= $in.max_age)
        )
      )
  ]
| unique_by([.plan_id, .pr_number])
| sort_by(.created_at)
| last // empty
