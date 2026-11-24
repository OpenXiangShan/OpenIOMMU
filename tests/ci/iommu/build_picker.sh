#!/usr/bin/env bash
# Build only the checked-out OpenIOMMU with the locked Picker SDK and Verilator.
set -euo pipefail
rtl_root=$1
picker_out=$2
sdk=$3
jobs=${4:-8}
model_threads=${5:-8}
[[ "$model_threads" =~ ^[1-9][0-9]*$ ]] || { echo "model threads must be positive" >&2; exit 1; }
[[ ! -e "$picker_out" ]] || { echo "picker output must be new" >&2; exit 1; }
mkdir -p "$(dirname "$picker_out")"

if ! command -v verible-verilog-syntax >/dev/null 2>&1; then
    echo "verible-verilog-syntax is required by Picker; set IOMMU_CI_VERIBLE_DIR or runner PATH" >&2
    exit 127
fi

"$sdk/bin/picker" export "$rtl_root/iommu_wrap.sv" \
    --sname iommu_wrap --tdir "$picker_out" --lang cpp --sim verilator \
    --autobuild false --cp_lib false -e \
    --vflag "--timing --threads $model_threads" \
    --cflag "-DENABLE_XCOROUTINE=true -I$sdk/share/picker/include" \
    --sdir "$sdk/share/picker/template"

python3 - "$rtl_root" "$picker_out" <<'PY'
from pathlib import Path
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
            continue  # Picker has generated a copy, included once below.
        line = str(path)
    lines.append(line)
(output / 'filelist.f').write_text('\n'.join(lines) + '\n')
# Adapt the locked SDK locally; do not mutate the downloaded tools or RTL.
# Verilator must consume the ordered filelist (including +define/+incdir)
# as simulator arguments, rather than as CMake source-file names.
path = output / 'cmake/verilator.cmake'
text = path.read_text()
text = text.replace('if(SIMULATOR STREQUAL "verilator")',
                    'if(SIMULATOR STREQUAL "verilator")\n'
                    '\tseparate_arguments(SIMULATOR_FLAGS UNIX_COMMAND "${SIMULATOR_FLAGS}")', 1)
text = text.replace('\t\t${FILELIST}\n', '', 1)
text = text.replace('\t\t-Wno-fatal\n', '\t\t-Wno-fatal\n'
                    '\t\t-f ${CMAKE_CURRENT_SOURCE_DIR}/filelist.f\n', 1)
path.write_text(text)
# The SDK's no-trace diagnostic lacks a newline. Preserve complete APB lines
# when stderr is merged with stdout; strict rejection checks remain unchanged.
path = output / 'dut_base.cpp'
path.write_text(path.read_text().replace(
    'std::cerr << "Verilator waveform is not enabled";',
    'std::cerr << "Verilator waveform is not enabled" << std::endl;'))
PY
make -C "$picker_out" build NPROC="$jobs"
make -C "$picker_out" install INSTALL_PREFIX="$picker_out" NPROC="$jobs"
cp -L "$sdk/share/picker/lib/libxspcomm.so" "$picker_out/libxspcomm.so"
for required in UT_iommu_wrap.hpp UT_iommu_wrap.cpp UT_iommu_wrap_dpi.hpp libUTiommu_wrap.so; do
    [[ -f "$picker_out/$required" ]] || { echo "missing picker output: $required" >&2; exit 1; }
done
