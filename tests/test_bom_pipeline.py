import os
import sys

import openpyxl

# Add parent directory to sys.path
sys.path.append(
    os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
)

from run_bom_pipeline import (  # noqa: E402
    QuadraticProbingHashTable,
    clean_currency_and_numbers,
    run_pipeline,
    split_combined_tags,
)


def test_custom_hash_table():
    ht = QuadraticProbingHashTable()
    ht.insert("key1", "val1")
    ht.insert("key2", "val2")
    assert ht.contains("key1")
    assert ht.get("key2") == "val2"
    assert not ht.contains("key3")

    # Test collisions and resizing
    for i in range(50):
        ht.insert(f"k{i}", f"v{i}")
    for i in range(50):
        assert ht.contains(f"k{i}")
        assert ht.get(f"k{i}") == f"v{i}"


def test_split_combined_tags():
    assert split_combined_tags("P-101A/B") == ["P-101A", "P-101B"]
    assert split_combined_tags("P-101/102/103") == [
        "P-101", "P-102", "P-103"
    ]
    assert split_combined_tags("XV-101A/B/C") == [
        "XV-101A", "XV-101B", "XV-101C"
    ]
    assert split_combined_tags("XV-101") == ["XV-101"]


def test_clean_currency_and_numbers():
    assert clean_currency_and_numbers("otIoNrRs 75,000") == "INR 75,000"
    assert clean_currency_and_numbers("INR 35,000") == "INR 35,000"
    assert clean_currency_and_numbers("$ 1,20,000") == "USD 1,20,000"
    assert clean_currency_and_numbers("100") == "INR 100"


def test_pipeline_execution():
    input_folder = "data/raw"
    output_path = "data/processed_test/bom_consolidated_test.xlsx"
    if os.path.exists(output_path):
        try:
            os.remove(output_path)
        except OSError:
            pass

    run_pipeline(input_folder, output_path)

    assert os.path.exists(output_path)
    wb = openpyxl.load_workbook(output_path)
    assert "Consolidated BOM" in wb.sheetnames

    # Test single file PDF to CSV output
    single_pdf = "data/raw/bom_sheet.pdf"
    csv_output = "data/processed_test/bom_single_test.csv"
    if os.path.exists(csv_output):
        try:
            os.remove(csv_output)
        except OSError:
            pass

    run_pipeline(single_pdf, csv_output)
    assert os.path.exists(csv_output)

    with open(csv_output, mode="r", encoding="utf-8") as f:
        lines = f.readlines()
        assert len(lines) >= 8
        assert "Level,Item No,Tag" in lines[0]


if __name__ == "__main__":
    print("Running BOM Pipeline Unit & Integration Tests...")
    test_custom_hash_table()
    print("- Custom Hash Table (Quadratic Probing) tests passed!")
    test_split_combined_tags()
    print("- Tag splitter (Regex) tests passed!")
    test_clean_currency_and_numbers()
    print("- Currency cleaning (Regex) tests passed!")
    test_pipeline_execution()
    print("- Pipeline integration tests passed!")
    print("--- ALL BOM PIPELINE TESTS PASSED SUCCESSFULLY ---")
