"""Refresh frozen research-asset checksums after intentional data/model changes."""
from pathlib import Path
from checksum_inventory import update_inventory

if __name__=='__main__':
    count=update_inventory(Path(__file__).resolve().parents[1])
    print(f'Updated checksums for {count} frozen research assets. Documentation/code are versioned by Git.')
