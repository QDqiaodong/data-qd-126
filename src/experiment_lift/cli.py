DESCRIPTION='Binary randomized experiment effect estimation with exact count audits'
import argparse, json, sys
from pathlib import Path
from .common import read, save
from .model import analyze

def run(source):
    data,digest=read(source)
    return {'schema_version':1,'source_sha256':digest,'result':analyze(data)}

def run_large(source,output):
    from .large import run as large_run
    return large_run(source,output)

def main(argv=None):
    parser=argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument('--source',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--large-snapshot',action='store_true',
                        help='stream an unordered snapshot of up to 1 GiB / 1,000,000 rows per class (requires SQLite scratch space next to the output)')
    args=parser.parse_args(argv)
    try:
        if args.source.resolve()==args.output.resolve():raise ValueError('input and output must differ')
        report=run_large(args.source,args.output) if args.large_snapshot else run(args.source)
        save(args.output,report)
    except (ValueError,TypeError,KeyError,OSError,OverflowError,RecursionError) as error:
        print(str(error),file=sys.stderr);return 2
    print(json.dumps(report,sort_keys=True,ensure_ascii=False,allow_nan=False));return 0

if __name__=='__main__':raise SystemExit(main())
