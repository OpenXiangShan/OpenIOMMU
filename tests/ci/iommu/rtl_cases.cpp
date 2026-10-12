// Real RTL tests using the QEMU/io-system IOMMU API. No stub/refmodel fallback.
#include "iommu_api.h"

#include <algorithm>
#include <array>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <stdexcept>
#include <string>
#include <vector>

static constexpr uint64_t BASE = 0x80000000;
static constexpr uint64_t DDT = BASE;
static constexpr uint64_t CQ = BASE + 0x10000;
static constexpr uint64_t FQ = BASE + 0x11000;
static constexpr uint64_t FENCE = BASE + 0x12000;
static constexpr uint64_t DATA1 = BASE + 0x80000;
static constexpr uint64_t DATA2 = BASE + 0x90000;
static constexpr uint64_t REMAP = BASE + 0xa0000;

static void require(bool condition, const std::string &message)
{
    if (!condition) {
        throw std::runtime_error(message);
    }
}

struct Memory {
    std::vector<uint8_t> bytes = std::vector<uint8_t>(0x200000);
    size_t reads = 0, writes = 0, atomics = 0;
    bool invalid = false;

    uint8_t *at(uint64_t address, size_t size)
    {
        require(address >= BASE && address - BASE <= bytes.size() &&
                size <= bytes.size() - (address - BASE), "memory range violation");
        return bytes.data() + (address - BASE);
    }
    void put64(uint64_t address, uint64_t value) { std::memcpy(at(address, 8), &value, 8); }
    uint64_t get64(uint64_t address)
    {
        uint64_t value;
        std::memcpy(&value, at(address, 8), 8);
        return value;
    }
    static int read(uint64_t address, uint8_t *data, size_t length, uint16_t,
                    size_t, size_t, const iommu_ace_lite_attrs_t *, void *opaque)
    {
        auto &memory = *static_cast<Memory *>(opaque);
        try {
            const uint64_t beat = address & ~(uint64_t(length) - 1);
            std::memcpy(data, memory.at(beat, length), length);
            ++memory.reads;
            std::printf("READ gpa=0x%llx size=%zu\n", (unsigned long long)beat, length);
            return 1;
        } catch (...) {
            memory.invalid = true;
            return 0;
        }
    }
    static void write(uint64_t address, const uint8_t *data, size_t length, uint32_t strobe,
                      uint16_t, size_t, size_t, const iommu_ace_lite_attrs_t *attrs, void *opaque)
    {
        auto &memory = *static_cast<Memory *>(opaque);
        try {
            const uint64_t beat = address & ~(uint64_t(length) - 1);
            auto *target = memory.at(beat, length);
            for (size_t i = 0; i < length; ++i) {
                if (strobe & (uint32_t(1) << i)) {
                    if (attrs && attrs->awatop == 0x13) {
                        target[i] |= data[i]; // ACE atomic bit-set for PTE A/D.
                    } else {
                        target[i] = data[i];
                    }
                }
            }
            ++memory.writes;
            memory.atomics += attrs && attrs->awatop == 0x13;
            std::printf("WRITE gpa=0x%llx size=%zu strobe=0x%x atop=0x%x\n",
                        (unsigned long long)beat, length, strobe, attrs ? attrs->awatop : 0);
        } catch (...) {
            memory.invalid = true;
        }
    }
};

struct Test {
    Memory memory;
    iommu_handle_t *handle = nullptr;
    std::array<uint64_t, 3> leaf{};

    Test()
    {
        handle = iommu_open();
        require(handle, "iommu_open failed");
        downstream_set_callbacks_v2(handle, Memory::write, Memory::read, &memory);
        translation_set_callbacks_v2(handle, Memory::write, Memory::read, &memory);
        write(0x1000, 0x80000000); // Disable reset-default hardware bypass.
    }
    ~Test() { if (handle) { iommu_close(handle); } }
    void step(int cycles = 256) { require(iommu_step(handle, cycles), "iommu_step failed"); }
    void write(uint32_t address, uint32_t value)
    {
        require(mmio_write(handle, address, value), "APB write failed at " + std::to_string(address));
        step(address >= 0x2b0 && address <= 0x2f8 ? 1024 : 256);
    }
    uint32_t read(uint32_t address)
    {
        step(64);
        uint32_t value = 0;
        require(mmio_read(handle, address, &value), "APB read failed at " + std::to_string(address));
        std::printf("REG 0x%x=0x%x\n", address, value);
        return value;
    }
    uint64_t read64(uint32_t address) { auto lo = read(address); return lo | (uint64_t(read(address + 4)) << 32); }
    void write64(uint32_t address, uint64_t value)
    {
        write(address + 4, value >> 32);
        write(address, uint32_t(value));
    }
    void poll(uint32_t address, uint32_t mask, uint32_t expected)
    {
        for (int i = 0; i < 128; ++i) {
            if ((read(address) & mask) == expected) { return; }
            step();
        }
        throw std::runtime_error("register poll timed out at " + std::to_string(address));
    }
    iommu_request_context_t context(uint32_t did) { return {did, 0, 0, 0}; }
    void tables(bool ad = false)
    {
        // Extended DDT format, S-stage Sv39, G-stage Bare, PDTV=0/PV=0.
        require(read64(0) & (uint64_t(1) << 22), "extended MSI context format unavailable");
        for (unsigned did = 1; did <= 2; ++did) {
            uint64_t root = BASE + 0x2000 + (did - 1) * 0x3000;
            leaf[did] = root + 0x2000;
            memory.put64(root, ((root + 0x1000) >> 12 << 10) | 1);
            memory.put64(root + 0x1000, (leaf[did] >> 12 << 10) | 1);
            auto payload = did == 1 ? DATA1 : DATA2;
            auto flags = uint64_t(did == 1 ? 0x17 : 0x13) | (ad ? 0 : 0xc0);
            memory.put64(leaf[did] + 8, (payload >> 12 << 10) | flags);
            memory.put64(leaf[did] + 16, ((payload + 0x1000) >> 12 << 10) | flags);
            memory.put64(DDT + did * 64, 1 | (ad ? 1 << 8 : 0));
            memory.put64(DDT + did * 64 + 16, uint64_t(did) << 12);
            memory.put64(DDT + did * 64 + 24, (uint64_t(8) << 60) | (root >> 12));
        }
        write64(0x10, (DDT >> 12 << 10) | 2);
        write(0x2b0, 0);
        write(0x2b0, 1); // Synchronize DDTP/configuration into ACD.
    }
    void queue(uint32_t base, uint32_t csr, uint64_t address)
    {
        write64(base, (address >> 12 << 10) | 2); // Eight entries.
        write(csr, 1);
        poll(csr, 0x10001, 0x10001);
    }
    void dma_roundtrip(uint64_t iova, uint64_t gpa, size_t size, uint32_t did)
    {
        std::vector<uint8_t> input(size), output(size);
        for (size_t i = 0; i < size; ++i) { input[i] = uint8_t(i * 37 + 19); }
        auto ctx = context(did);
        auto *target = memory.at(gpa - 1, size + 2);
        std::fill(target, target + size + 2, 0xa5);
        require(memory_write_with_context(handle, iova, input.data(), size, &ctx), "DMA write failed");
        require(!std::memcmp(memory.at(gpa, size), input.data(), size), "wrong DMA GPA/data");
        require(target[0] == 0xa5 && target[size + 1] == 0xa5, "strobe corrupted guard bytes");
        require(memory_read_with_context(handle, iova, output.data(), size, &ctx), "DMA read failed");
        require(input == output, "DMA read mismatch");
    }
};

static void runtime(Test &test)
{
    require(test.read64(0) == 0x1f8c7e68610ULL, "capability mismatch");
    test.step(128);
}

static void apb(Test &test)
{
    runtime(test);
    require(test.read64(0x10) == 0, "DDTP reset mismatch");
    auto caps = test.read64(0);
    require(!mmio_write(test.handle, 0, 0xffffffff), "read-only capability write was accepted");
    std::puts("EXPECTED_APB_REJECT addr=0x00000000 data=0xffffffff");
    require(test.read64(0) == caps, "capability register is writable");
    test.write(0x308, 0x5aa55aa5); // MSI vector data, safe while interrupts disabled.
    require(test.read(0x308) == 0x5aa55aa5, "MSI data readback mismatch");
    uint64_t ddtp = (uint64_t(0x12345) << 32) | 0x23456002;
    test.write64(0x10, ddtp);
    require(test.read64(0x10) == ddtp, "64-bit DDTP readback mismatch");
}

static void translate(Test &test)
{
    require(test.read64(0) & (uint64_t(1) << 21), "AMO A/D capability required");
    test.tables(true);
    test.dma_roundtrip(0x1ff1, DATA1 + 0xff1, 96, 1);
    require((test.memory.get64(test.leaf[1] + 8) & 0xc0) == 0xc0, "first-page A/D update missing");
    require((test.memory.get64(test.leaf[1] + 16) & 0xc0) == 0xc0, "second-page A/D update missing");
    require(test.memory.atomics > 0, "A/D path did not issue atomic updates");
    // A second DID maps the same IOVA to a distinct physical page.
    test.memory.at(DATA2, 1)[0] = 0x6e;
    auto ctx = test.context(2);
    uint8_t value = 0;
    require(memory_read_with_context(test.handle, 0x1000, &value, 1, &ctx) && value == 0x6e,
            "DID isolation/read-only mapping failed");
}

static void fault(Test &test)
{
    test.tables();
    test.queue(0x28, 0x4c, FQ);
    const std::array<uint32_t, 3> dids{9, 1, 2};
    const std::array<uint64_t, 3> iovas{0x1000, 0x5000, 0x1000};
    const std::array<uint32_t, 3> causes{258, 15, 15};
    std::vector<uint8_t> saved(test.memory.at(DATA1, 0x21000), test.memory.at(DATA1, 0x21000) + 0x21000);
    for (size_t i = 0; i < dids.size(); ++i) {
        auto ctx = test.context(dids[i]);
        std::array<uint8_t, 8> data{1, 2, 3, 4, 5, 6, 7, 8};
        require(!memory_write_with_context(test.handle, iovas[i], data.data(), data.size(), &ctx),
                "faulting DMA unexpectedly succeeded");
        test.poll(0x34, 7, uint32_t(i + 1));
        auto record = test.memory.get64(FQ + i * 32);
        require((record & 0xfff) == causes[i], "wrong FQ cause");
        require((record >> 40) == dids[i], "wrong FQ DID");
        require(((record >> 34) & 0x3f) == 3, "wrong FQ transaction type");
        require(test.memory.get64(FQ + i * 32 + 16) == iovas[i], "wrong FQ IOVA");
        std::printf("EXPECTED_FAULT cause=%u did=%u iova=0x%llx\n", causes[i], dids[i],
                    (unsigned long long)iovas[i]);
        test.write(0x30, uint32_t(i + 1));
    }
    require(!std::memcmp(saved.data(), test.memory.at(DATA1, saved.size()), saved.size()),
            "faulting DMA modified destination");
    require(!(test.read(0x4c) & 0x300), "FQ memory fault/overflow");
}

static void invalidate(Test &test)
{
    test.tables();
    test.queue(0x18, 0x48, CQ);
    auto ctx = test.context(1);
    uint8_t value = 0;
    test.memory.at(DATA1, 1)[0] = 0x11;
    test.memory.at(REMAP, 1)[0] = 0x77;
    require(memory_read_with_context(test.handle, 0x1000, &value, 1, &ctx) && value == 0x11,
            "initial mapping failed");
    test.memory.put64(test.leaf[1] + 8, (REMAP >> 12 << 10) | 0xd7);
    test.memory.put64(CQ, 1); // IOTINVAL.VMA, all addresses/contexts.
    test.memory.put64(CQ + 16, 2 | (uint64_t(1) << 10) | (uint64_t(1) << 12) |
                             (uint64_t(1) << 13) | (uint64_t(0x87654321) << 32));
    test.memory.put64(CQ + 24, FENCE >> 2); // IOFENCE.C completion write.
    test.write(0x24, 2);
    test.poll(0x20, 7, 2);
    require(!(test.read(0x48) & 0x700), "CQ command/timeout/memory error");
    require(uint32_t(test.memory.get64(FENCE)) == 0x87654321, "IOFENCE completion write missing");
    require(memory_read_with_context(test.handle, 0x1000, &value, 1, &ctx) && value == 0x77,
            "stale translation after IOTINVAL");
    test.dma_roundtrip(0x1013, REMAP + 0x13, 53, 1);
    require(test.memory.at(DATA1, 1)[0] == 0x11, "old mapping was modified");
}

int main(int argc, char **argv)
{
    if (argc != 3) { std::fprintf(stderr, "usage: rtl-cases CASE NONCE\n"); return 2; }
    const std::string id = argv[1];
    try {
        {
            Test test;
            if (id == "picker-runtime") { runtime(test); }
            else if (id == "rtl-apb") { apb(test); }
            else if (id == "rtl-dma-bypass") {
                test.write(0x1000, 0x80000001);
                test.dma_roundtrip(DATA1 + 0xff1, DATA1 + 0xff1, 96, 1);
                test.dma_roundtrip(DATA1 + 3, DATA1 + 3, 63, 1);
            } else if (id == "rtl-dma-translate") { translate(test); }
            else if (id == "rtl-dma-fault") { fault(test); }
            else if (id == "rtl-invalidate") { invalidate(test); }
            else { throw std::runtime_error("unknown case"); }
            require(!test.memory.invalid, "callback accessed unmapped guest memory");
        }
        std::printf("IOMMU_CI_PASS:%s:%s\n", id.c_str(), argv[2]);
        return 0;
    } catch (const std::exception &error) {
        std::fprintf(stderr, "IOMMU_CI_FAIL:%s:%s\n", id.c_str(), error.what());
        return 1;
    }
}
