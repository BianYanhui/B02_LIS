# AI Master Table v2

Each row is a raw experiment cell or summary cell. `source_dataset` identifies its native metric schema; do not compare metrics with different source datasets unless their definitions match in `data_dictionary_v2.csv`.

Use only rows where `status=Current` as primary paper evidence. Evidence types distinguish live T4/vLLM data, fixed-snapshot trace replay, microbenchmarks, and control-plane simulations. `legacy_data_status.csv` records older sheets that must not be cited.
