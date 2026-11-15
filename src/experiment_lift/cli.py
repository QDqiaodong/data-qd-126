DESCRIPTION='Binary randomized experiment effect estimation with exact count audits'
import argparse, json, sqlite3, sys
from pathlib import Path
from .common import read, save
from .model import analyze

LEDGER_COMMANDS = ('init', 'assign', 'export')


def run(source):
    data,digest=read(source)
    return {'schema_version':1,'source_sha256':digest,'result':analyze(data)}

def run_large(source,output):
    from .large import run as large_run
    return large_run(source,output)

def _ledger_parser():
    parser=argparse.ArgumentParser(prog='experiment-lift',description=DESCRIPTION)
    commands=parser.add_subparsers(dest='command',required=True)
    init=commands.add_parser('init',help='create a ledger with fixed arms, treatment share and seed')
    init.add_argument('--ledger',type=Path,required=True)
    init.add_argument('--control',required=True)
    init.add_argument('--treatment',required=True)
    init.add_argument('--numerator',type=int,required=True,help='treatment share numerator (0 < numerator/denominator < 1)')
    init.add_argument('--denominator',type=int,required=True,help='treatment share denominator')
    init.add_argument('--seed',required=True)
    assign=commands.add_parser('assign',help='commit one batch of users and export the ledger')
    assign.add_argument('--ledger',type=Path,required=True)
    assign.add_argument('--source',type=Path,required=True,help='strict JSON: {"batch_id": ..., "users": [...]}, at most 20000 users')
    assign.add_argument('--output',type=Path,required=True,help='exported analysis-ready assignments')
    export=commands.add_parser('export',help='export the committed ledger as analysis-ready assignments')
    export.add_argument('--ledger',type=Path,required=True)
    export.add_argument('--output',type=Path,required=True)
    return parser


def ledger_main(argv):
    from . import ledger
    args=_ledger_parser().parse_args(argv)
    try:
        if args.command=='init':
            summary=ledger.init_ledger(args.ledger,args.control,args.treatment,
                                       args.numerator,args.denominator,args.seed)
        elif args.command=='assign':
            if args.source.resolve()==args.output.resolve():
                raise ValueError('input and output must differ')
            if args.ledger.resolve()==args.output.resolve():
                raise ValueError('ledger and output must differ')
            if args.output.is_symlink():
                raise ValueError('output cannot be a symlink')
            batch_id,users=ledger.read_batch(args.source)
            summary=ledger.assign_batch(args.ledger,batch_id,users)
            summary['exported']=ledger.export_ledger(args.ledger,args.output)
        else:
            summary=ledger.export_ledger(args.ledger,args.output)
    except (ValueError,TypeError,KeyError,OSError,OverflowError,RecursionError,sqlite3.Error) as error:
        print(str(error),file=sys.stderr);return 2
    print(json.dumps(summary,sort_keys=True,ensure_ascii=True,allow_nan=False));return 0


def main(argv=None):
    arguments=sys.argv[1:] if argv is None else list(argv)
    if arguments and arguments[0] in LEDGER_COMMANDS:
        return ledger_main(arguments)
    parser=argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument('--source',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--large-snapshot',action='store_true',
                        help='stream an unordered snapshot of up to 1 GiB / 1,000,000 rows per class (requires SQLite scratch space next to the output)')
    args=parser.parse_args(arguments)
    try:
        if args.source.resolve()==args.output.resolve():raise ValueError('input and output must differ')
        report=run_large(args.source,args.output) if args.large_snapshot else run(args.source)
        save(args.output,report)
    except (ValueError,TypeError,KeyError,OSError,OverflowError,RecursionError) as error:
        print(str(error),file=sys.stderr);return 2
    print(json.dumps(report,sort_keys=True,ensure_ascii=False,allow_nan=False));return 0

if __name__=='__main__':raise SystemExit(main())
