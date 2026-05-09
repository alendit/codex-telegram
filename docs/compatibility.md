# Compatibility Notes

This project still carries some compatibility names from earlier private
deployments so existing persisted state can be migrated deliberately.

- `/abort` and `/stop` remain aliases for `/interrupt`.
- `/cd` and `/cwd` remain aliases for `/dir`.
- `/clearparams` remains an alias for `/resetparams`.
- `LogicalThread` remains as a Telegram-facing compatibility shape around the
  newer anchor and bridge-window model.

Remove compatibility paths only with a migration or explicit operator decision,
and update tests in the same change.
