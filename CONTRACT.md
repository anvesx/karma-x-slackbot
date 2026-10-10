# Karma-X contract

Exactly one `/karma-x <@U…> ++ reason` adds one recognition. Count is a durable integer.
Signed Slack actor identity is authoritative; self-recognition, bots, deleted members, foreign workspace, and unapproved channels are rejected.
Top 30 ranks uncanceled totals with real recognition-message permalinks; ties share rank.
Winner commands use the caller's IANA timezone, UTC storage, and half-open calendar intervals. All tied maxima win. Cancellation retroactively removes recognition from rankings.
View defaults to your recent history; view ID must respect original channel membership. Only the original giver cancels. Cancel twice decrements once.
Exact visible `person 🌟2` means Slack display_name: use users.profile.set user scope, verify permission and actual rendering. Failure does not erase durable recognition or pretend a profile write succeeded.
No dashboard, setup command, reward economy, analytics, adjustable point amounts, or extra icons.
Transport retries are idempotent, cancellation transactional, network delivery outside write locks. SQLite+Slack are not a distributed transaction: ambiguous posts remain explicit and are not blindly repeated.
Local live testing uses the existing alias-bot-test channel. Unit tests only use temporary databases and fake transports. Never commit credentials, database files, or private channel exports.
