# Real-data example

`data/countries/Albania/summary_master_Albania.csv.gz` is the complete, unmodified Albania country master from the corrected CO₂ campaign. It contains 17 country–cells and 78,200 raw paired records. It is included so the processing workflow can be tested without downloading the complete archive.

From the repository root:

```bash
python3 -m pip install -r requirements.txt
python3 workflow/verify_repository.py
python3 workflow/analysis/run_analysis.py --data-root examples/data --countries Albania --output results/example --workers 1
python3 workflow/analysis/check_example.py --output results/example
```

Expected: 78,005 retained records and 195 excluded records under the documented yield filter. Raw input records are unchanged. This tests raw-output processing; it does not rerun the crop model.
