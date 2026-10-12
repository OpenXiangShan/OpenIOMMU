#!/usr/bin/env bash
# Build only the checked-out OpenIOMMU; the caller owns VCS environment/lock.
set -euo pipefail
rtl_root=$1
picker_out=$2
sdk=$3
jobs=${4:-8}
[[ ! -e "$picker_out" ]] || { echo "picker output must be new" >&2; exit 1; }
mkdir -p "$(dirname "$picker_out")"

"$sdk/bin/picker" export "$rtl_root/iommu_wrap.sv" \
    --sname iommu_wrap --tdir "$picker_out" --lang cpp --sim vcs \
    --autobuild false --cp_lib false -e \
    --cflag "-DENABLE_XCOROUTINE=true -I$sdk/share/picker/include" \
    --sdir "$sdk/share/picker/template"

python3 - "$rtl_root" "$picker_out" <<'PY'
from pathlib import Path
import re
import sys
root, output = map(Path, sys.argv[1:])
lines = ['+define+CONFIG_RISCV_IOMMU_BOSC_V2_LITE']
for line in (root / 'iommu_wrap.f').read_text().splitlines():
    line = line.strip()
    if not line or line.startswith('//'):
        continue
    if line.startswith('+incdir+'):
        line = '+incdir+' + '+'.join(str((root / p).resolve()) for p in line[8:].split('+'))
    elif line.startswith('+'):
        pass
    else:
        path = (root / line).resolve()
        if not path.is_file() or not path.is_relative_to(root):
            raise SystemExit('invalid source in filelist: ' + line)
        if path.name == 'iommu_wrap.sv':
            continue  # Picker has generated a copy, included once by VCS.
        line = str(path)
    lines.append(line)
(output / 'filelist.f').write_text('\n'.join(lines) + '\n')
for path in list(output.rglob('*.cmake')) + list(output.rglob('CMakeLists.txt')):
    text = path.read_text()
    text = re.sub(r'(?m)^(\s*)vcs -e VcsMain', r'\1bash vcs -e VcsMain', text)
    text = text.replace('${ModuleName}_top.sv ${ModuleName}.v -f filelist.f',
                        '-f filelist.f ${ModuleName}.v ${ModuleName}_top.sv')
    text = text.replace('-LDFLAGS "-shared', '-LDFLAGS "-shared -Wl,--no-as-needed')
    path.write_text(text)
# VCS $finish must not terminate the embedding process before its test marker.
path = output / 'dut_base.cpp'
text, count = re.subn(r'int DutVcsBase::Finish\(\)\s*\{.*?\n\};',
                     'int DutVcsBase::Finish()\n{\n    return 0; // Host owns process lifetime.\n};',
                     path.read_text(), count=1, flags=re.S)
if count != 1:
    raise SystemExit('unrecognized Picker Finish implementation')
path.write_text(text)
PY
make -C "$picker_out" build NPROC="$jobs"
make -C "$picker_out" install INSTALL_PREFIX="$picker_out"
cp -L "$sdk/share/picker/lib/libxspcomm.so" "$picker_out/libxspcomm.so"
for required in UT_iommu_wrap.hpp UT_iommu_wrap.cpp libUTiommu_wrap.so libDPIiommu_wrap.so; do
    [[ -f "$picker_out/$required" ]] || { echo "missing picker output: $required" >&2; exit 1; }
done
