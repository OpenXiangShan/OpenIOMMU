`timescale 1ns/1ps

// Regression test: fqcsr.fqon and cqcsr.cqon may read 0 (with busy 0) only
// when no fault-record write or command fetch is in flight and none can
// start.
//
// The complete iommu_wrap is driven only through its public ports. Software
// clears fqen (cqen) while a fault record is pending (commands remain to be
// fetched), polls fqcsr (cqcsr) until the queue reads off and not busy, and
// then may reuse the queue memory. The window is one clock cycle wide, so
// the race scenarios sweep the moment of the disable write over a range of
// cycles (+delay=<n>); each value is a separate simulation.
//
// Run with tests/regression/atd_sw_queue_off/run.py; select a scenario with
// +scenario=<name> +delay=<n>.
module tb_atd_sw_queue_off;
  // ===================================================================
  // Environment around iommu_wrap. Every model uses only the wrapper's
  // public ports and behaves legally for its interface.
  //   APB      register accesses (one at a time)
  //   DS port  data-structure memory: DDT, page tables, CQ, FQ. Reads are
  //            answered in order after ds_lat cycles. Writes can be held
  //            (W not accepted, or W accepted and B held) and can get SLVERR.
  //   MST port system memory behind the IOMMU. B responses can be held.
  //   SLV port device DMA writes, DID/PID/translated in awuser.
  //   DTI      ATS endpoint: connect, answers INV/SYNC requests (optionally
  //            withheld until released).
  // ===================================================================
  localparam logic [31:0] REG_DDTP    = 32'h10;
  localparam logic [31:0] REG_CQB     = 32'h18;
  localparam logic [31:0] REG_CQB_HI  = 32'h1c;
  localparam logic [31:0] REG_CQH     = 32'h20;
  localparam logic [31:0] REG_CQT     = 32'h24;
  localparam logic [31:0] REG_FQB     = 32'h28;
  localparam logic [31:0] REG_FQB_HI  = 32'h2c;
  localparam logic [31:0] REG_FQH     = 32'h30;
  localparam logic [31:0] REG_FQT     = 32'h34;
  localparam logic [31:0] REG_CQCSR   = 32'h48;
  localparam logic [31:0] REG_FQCSR   = 32'h4c;
  localparam logic [31:0] REG_IPSR    = 32'h54;
  localparam logic [31:0] REG_CTRL    = 32'h1000;   // wrapper control (0: no bypass)

  localparam logic [63:0] CQ_BASE   = 64'h1000;     // 64 entries
  localparam int          CQ_ENTRIES = 64;
  localparam logic [63:0] DDT_BASE  = 64'h2000;     // one-level DDT, 64-byte contexts
  localparam logic [63:0] PT_BASE   = 64'h10000;    // Sv39 tables per device: root, l1, l0
  localparam logic [63:0] DATA_BASE = 64'h40000;    // data pages in system memory
  localparam logic [63:0] SV39_IOVA = 64'h4000_0000; // 1 GiB: root[1] -> l1[0] -> l0[slot]

  localparam logic [63:0] PTE_V = 64'h1, PTE_R = 64'h2, PTE_W = 64'h4, PTE_U = 64'h10,
                          PTE_A = 64'h40, PTE_D = 64'h80;
  localparam logic [63:0] LEAF_RW = PTE_V | PTE_R | PTE_W | PTE_U | PTE_A | PTE_D;

  logic clk = 1'b0;
  always #5 clk = ~clk;
  logic rstn = 1'b0;
  int cycle = 0;
  always @(posedge clk) if (rstn) cycle = cycle + 1;

  bit verbose = 0;
  string scenario = "";

  // ----------------------------------------------------------------- APB
  logic penable = 0, pwrite = 0, psel = 0;
  logic [31:0] paddr = 0, pwdata = 0;
  wire [31:0] prdata;
  wire pready, pslverr;

  // ---------------------------------------------------------- device AXI
  logic        slv_awvalid = 0;  wire slv_awready;
  logic [9:0]  slv_awid = 0;     logic [39:0] slv_awaddr = 0;
  logic [53:0] slv_awuser = 0;
  logic        slv_wvalid = 0;   wire slv_wready;
  logic [255:0] slv_wdata = 0;
  wire         slv_bvalid;       wire [9:0] slv_bid;  wire [1:0] slv_bresp;
  wire         slv_rvalid;       wire [9:0] slv_rid;  wire [255:0] slv_rdata;
  wire [1:0]   slv_rresp;        wire slv_rlast;

  // ---------------------------------------------------------- memory AXI
  wire        mst_awvalid; wire [9:0] mst_awid; wire [39:0] mst_awaddr; wire [7:0] mst_awlen;
  wire        mst_wvalid;  wire [255:0] mst_wdata; wire [31:0] mst_wstrb; wire mst_wlast;
  wire        mst_bready;
  wire        mst_arvalid; wire [9:0] mst_arid; wire [39:0] mst_araddr; wire [7:0] mst_arlen;
  wire        mst_rready;
  logic       mst_wready_q = 0;
  logic       mst_bvalid_q = 0;  logic [9:0] mst_bid_q = 0;
  logic       mst_rvalid_q = 0;  logic [9:0] mst_rid_q = 0; logic mst_rlast_q = 0;

  // ----------------------------------------------------------------- DTI
  logic tr_rvalid = 0; wire tr_rready; logic [63:0] tr_rdata = 0; logic tr_rlast = 0;
  wire tr_tvalid; wire [63:0] tr_tdata; wire tr_tlast;

  // ------------------------------------------------------------------ DS
  wire ds_arvalid; wire [5:0] ds_arid; wire [55:0] ds_araddr; wire [7:0] ds_arlen; wire ds_rready;
  logic ds_rvalid_q = 0; logic [5:0] ds_rid_q = 0; logic [255:0] ds_rdata_q = 0; logic ds_rlast_q = 0;
  wire ds_awvalid; wire [5:0] ds_awid; wire [55:0] ds_awaddr; wire [7:0] ds_awlen;
  wire ds_wvalid; wire [255:0] ds_wdata; wire [31:0] ds_wstrb; wire ds_wlast; wire ds_bready;
  logic ds_wready_q = 0, ds_bvalid_q = 0; logic [5:0] ds_bid_q = 0; logic [1:0] ds_bresp_q = 0;

  iommu_wrap dut (
    .iommu_clk(clk), .iommu_rstn(rstn),
    .iommu_penable_i(penable), .iommu_pwrite_i(pwrite), .iommu_paddr_i(paddr),
    .iommu_psel_i(psel), .iommu_pwdata_i(pwdata), .iommu_prdata_o(prdata),
    .iommu_pready_o(pready), .iommu_pslverr_o(pslverr),

    .iommu_slv_awid(slv_awid), .iommu_slv_awaddr(slv_awaddr), .iommu_slv_awlen(8'd0),
    .iommu_slv_awsize(3'd5), .iommu_slv_awburst(2'b01), .iommu_slv_awlock(1'b0),
    .iommu_slv_awcache('0), .iommu_slv_awprot('0), .iommu_slv_awregion('0),
    .iommu_slv_awuser(slv_awuser), .iommu_slv_awvalid(slv_awvalid), .iommu_slv_awready(slv_awready),
    .iommu_slv_awsnoop('0), .iommu_slv_awdomain('0), .iommu_slv_awidunq(1'b0),
    .iommu_slv_awatop('0), .iommu_slv_awloop(1'b0),
    .iommu_slv_arid('0), .iommu_slv_araddr('0), .iommu_slv_arlen(8'd0),
    .iommu_slv_arsize(3'd5), .iommu_slv_arburst(2'b01), .iommu_slv_arlock(1'b0),
    .iommu_slv_arcache('0), .iommu_slv_arprot('0), .iommu_slv_arregion('0),
    .iommu_slv_aruser('0), .iommu_slv_arvalid(1'b0), .iommu_slv_arready(),
    .iommu_slv_arsnoop('0), .iommu_slv_ardomain('0), .iommu_slv_aridunq(1'b0), .iommu_slv_arloop(1'b0),
    .iommu_slv_wdata(slv_wdata), .iommu_slv_wstrb({32{1'b1}}), .iommu_slv_wlast(1'b1),
    .iommu_slv_wvalid(slv_wvalid), .iommu_slv_wready(slv_wready), .iommu_slv_wuser('0),
    .iommu_slv_rready(1'b1), .iommu_slv_bready(1'b1),
    .iommu_slv_awqos('0), .iommu_slv_arqos('0),
    .iommu_slv_rid(slv_rid), .iommu_slv_rdata(slv_rdata), .iommu_slv_rresp(slv_rresp),
    .iommu_slv_rlast(slv_rlast), .iommu_slv_rvalid(slv_rvalid),
    .iommu_slv_bid(slv_bid), .iommu_slv_bvalid(slv_bvalid), .iommu_slv_bresp(slv_bresp),

    .iommu_mst_awid(mst_awid), .iommu_mst_awaddr(mst_awaddr), .iommu_mst_awlen(mst_awlen),
    .iommu_mst_awvalid(mst_awvalid), .iommu_mst_awready(1'b1),
    .iommu_mst_wvalid(mst_wvalid), .iommu_mst_wdata(mst_wdata), .iommu_mst_wstrb(mst_wstrb),
    .iommu_mst_wlast(mst_wlast), .iommu_mst_wready(mst_wready_q),
    .iommu_mst_bid(mst_bid_q), .iommu_mst_bvalid(mst_bvalid_q), .iommu_mst_bready(mst_bready),
    .iommu_mst_bresp(2'b00), .iommu_mst_buser('0), .iommu_mst_bidunq(1'b0), .iommu_mst_bloop(1'b0),
    .iommu_mst_arid(mst_arid), .iommu_mst_araddr(mst_araddr), .iommu_mst_arlen(mst_arlen),
    .iommu_mst_arvalid(mst_arvalid), .iommu_mst_arready(1'b1),
    .iommu_mst_rid(mst_rid_q), .iommu_mst_rdata('0), .iommu_mst_rresp(2'b00),
    .iommu_mst_rlast(mst_rlast_q), .iommu_mst_rvalid(mst_rvalid_q), .iommu_mst_rready(mst_rready),
    .iommu_mst_ruser('0), .iommu_mst_ridunq(1'b0), .iommu_mst_rloop(1'b0),

    .iommu_tr_rvalid_i(tr_rvalid), .iommu_tr_rready_o(tr_rready), .iommu_tr_rdata_i(tr_rdata),
    .iommu_tr_rstrb_i(8'hff), .iommu_tr_rkeep_i(8'hff), .iommu_tr_rlast_i(tr_rlast),
    .iommu_tr_rid_i(4'd0), .iommu_tr_tvalid_o(tr_tvalid), .iommu_tr_tready_i(1'b1),
    .iommu_tr_tdata_o(tr_tdata), .iommu_tr_tlast_o(tr_tlast),

    .iommu_ds_acvalid(1'b0), .iommu_ds_acaddr('0), .iommu_ds_acvmidext('0),
    .iommu_ds_acsnoop('0), .iommu_ds_acprot('0), .iommu_ds_crready(1'b1),
    .iommu_ds_awready(1'b1), .iommu_ds_arready(1'b1), .iommu_ds_wready(ds_wready_q),
    .iommu_ds_awid(ds_awid), .iommu_ds_awaddr(ds_awaddr), .iommu_ds_awlen(ds_awlen),
    .iommu_ds_awvalid(ds_awvalid), .iommu_ds_wdata(ds_wdata), .iommu_ds_wstrb(ds_wstrb),
    .iommu_ds_wlast(ds_wlast), .iommu_ds_wvalid(ds_wvalid), .iommu_ds_bready(ds_bready),
    .iommu_ds_rid(ds_rid_q), .iommu_ds_rdata(ds_rdata_q), .iommu_ds_rresp(2'b00),
    .iommu_ds_rlast(ds_rlast_q), .iommu_ds_rvalid(ds_rvalid_q),
    .iommu_ds_bid(ds_bid_q), .iommu_ds_bvalid(ds_bvalid_q), .iommu_ds_bresp(ds_bresp_q),
    .iommu_ds_arvalid(ds_arvalid), .iommu_ds_arid(ds_arid), .iommu_ds_araddr(ds_araddr),
    .iommu_ds_arlen(ds_arlen), .iommu_ds_rready(ds_rready)
  );

  // ------------------------------------------------------------ memories
  logic [63:0] dsmem [logic [63:0]];
  logic [63:0] sysmem [logic [63:0]];

  function automatic void poke(input logic [63:0] addr, input logic [63:0] data);
    dsmem[{addr[63:3], 3'b000}] = data;
  endfunction

  function automatic logic [63:0] peek(input logic [63:0] addr);
    logic [63:0] k;
    k = {addr[63:3], 3'b000};
    return dsmem.exists(k) ? dsmem[k] : 64'd0;
  endfunction

  function automatic logic [255:0] ds_line(input logic [63:0] a);
    logic [255:0] v;
    for (int i = 0; i < 4; i++) v[64*i +: 64] = peek({a[63:5], 5'b0} + 64'(8 * i));
    return v;
  endfunction

  // --------------------------------------------------------------- DS port
  int ds_lat = 0;
  typedef struct { logic [5:0] id; logic [63:0] addr; logic [7:0] len; int ready_at;
                   logic [255:0] data [16]; } ds_rd_t;
  ds_rd_t ds_q[$];
  int ds_beat = 0;
  typedef struct { logic [5:0] id; logic [63:0] addr; } ds_aw_t;
  ds_aw_t ds_aw_q[$];
  int ds_w_beat = 0;
  typedef struct { logic [5:0] id; logic [63:0] addr; logic [1:0] resp; } ds_b_t;
  ds_b_t ds_b_q[$];
  // write hold window: mode 0 holds W acceptance, mode 1 accepts W and holds B
  bit dsw_on = 0; logic [63:0] dsw_lo = 0, dsw_hi = 0; int dsw_mode = 0;
  // B error window: writes in [dsb_lo, dsb_hi) get SLVERR
  bit dsb_err_on = 0; logic [63:0] dsb_lo = 0, dsb_hi = 0;
  // write log
  int          dsw_n = 0;
  int          dsw_aw_cycle [$];  logic [63:0] dsw_aw_addr [$];
  int          dsw_w_cycle  [$];  logic [63:0] dsw_w_addr  [$];
  int          dsw_b_cycle  [$];  logic [63:0] dsw_b_addr  [$];
  int          dsr_ar_cycle [$];  logic [63:0] dsr_ar_addr [$];
  int          dsr_r_cycle  [$];  logic [63:0] dsr_r_addr  [$];

  function automatic bit dsw_in(input logic [63:0] a);
    return dsw_on && a >= dsw_lo && a < dsw_hi;
  endfunction

  always @(posedge clk) begin
    if (rstn) begin
      if (ds_arvalid) begin
        ds_rd_t e;
        e.id = ds_arid; e.addr = 64'(ds_araddr); e.len = ds_arlen;
        e.ready_at = cycle + ds_lat;
        for (int b = 0; b < 16; b++) e.data[b] = (b <= int'(e.len)) ? ds_line(e.addr + 64'(32 * b)) : '0;
        ds_q.push_back(e);
        dsr_ar_cycle.push_back(cycle); dsr_ar_addr.push_back(e.addr);
        if (verbose) $display("[%0d] DS AR addr=%h len=%0d", cycle, e.addr, e.len);
      end
      if (ds_rvalid_q && ds_rready) begin
        if (ds_rlast_q) begin
          dsr_r_cycle.push_back(cycle); dsr_r_addr.push_back(ds_q[0].addr);
          void'(ds_q.pop_front());
          ds_beat = 0;
        end else ds_beat++;
      end
      if (ds_awvalid) begin
        ds_aw_t a; a.id = ds_awid; a.addr = 64'(ds_awaddr);
        ds_aw_q.push_back(a);
        dsw_aw_cycle.push_back(cycle); dsw_aw_addr.push_back(a.addr);
        if (verbose) $display("[%0d] DS AW addr=%h", cycle, a.addr);
      end
      if (ds_wvalid && ds_wready_q) begin
        logic [63:0] wa, k, old;
        wa = {ds_aw_q[0].addr[63:5], 5'b0} + 64'(32 * ds_w_beat);
        for (int i = 0; i < 4; i++) begin
          k = wa + 64'(8 * i);
          old = peek(k);
          for (int b = 0; b < 8; b++) if (ds_wstrb[8*i + b]) old[8*b +: 8] = ds_wdata[64*i + 8*b +: 8];
          if (ds_wstrb[8*i +: 8] != 0) dsmem[k] = old;
        end
        if (ds_wlast) begin
          ds_b_t bb;
          bb.id = ds_aw_q[0].id; bb.addr = ds_aw_q[0].addr;
          bb.resp = (dsb_err_on && bb.addr >= dsb_lo && bb.addr < dsb_hi) ? 2'b10 : 2'b00;
          ds_b_q.push_back(bb);
          dsw_w_cycle.push_back(cycle); dsw_w_addr.push_back(bb.addr);
          if (verbose) $display("[%0d] DS W  addr=%h", cycle, bb.addr);
          void'(ds_aw_q.pop_front());
          ds_w_beat = 0;
        end else ds_w_beat++;
      end
      if (ds_bvalid_q && ds_bready) begin
        dsw_b_cycle.push_back(cycle); dsw_b_addr.push_back(ds_b_q[0].addr);
        if (verbose) $display("[%0d] DS B  addr=%h resp=%0d", cycle, ds_b_q[0].addr, ds_b_q[0].resp);
        void'(ds_b_q.pop_front());
      end
      if (ds_q.size() != 0 && ds_q[0].ready_at <= cycle) begin
        ds_rvalid_q <= 1; ds_rid_q <= ds_q[0].id; ds_rdata_q <= ds_q[0].data[ds_beat];
        ds_rlast_q <= (ds_beat == int'(ds_q[0].len));
      end else begin
        ds_rvalid_q <= 0; ds_rlast_q <= 0;
      end
      ds_wready_q <= (ds_aw_q.size() != 0) && !(dsw_mode == 0 && dsw_in(ds_aw_q[0].addr));
      ds_bvalid_q <= (ds_b_q.size() != 0) && !(dsw_mode == 1 && dsw_in(ds_b_q[0].addr));
      ds_bid_q    <= (ds_b_q.size() != 0) ? ds_b_q[0].id : '0;
      ds_bresp_q  <= (ds_b_q.size() != 0) ? ds_b_q[0].resp : 2'b00;
    end
  end

  // ----------------------------------------------------- system memory port
  bit mst_b_hold = 0;                      // withhold every B response
  typedef struct { logic [9:0] id; logic [63:0] addr; } mst_aw_t;
  mst_aw_t m_aw_q[$];
  logic [9:0] m_b_q[$];
  logic [9:0] m_ar_q[$];
  int          mst_aw_cycle [$]; logic [63:0] mst_aw_addr [$];
  int          mst_w_cycle  [$]; logic [63:0] mst_w_addr  [$]; logic [255:0] mst_w_data [$];
  int          mst_b_count = 0;

  always @(posedge clk) begin
    if (rstn) begin
      if (mst_awvalid) begin
        mst_aw_t a; a.id = mst_awid; a.addr = 64'(mst_awaddr);
        m_aw_q.push_back(a);
        mst_aw_cycle.push_back(cycle); mst_aw_addr.push_back(a.addr);
        if (verbose) $display("[%0d] MST AW addr=%h", cycle, a.addr);
      end
      if (mst_wvalid && mst_wready_q) begin
        if (mst_wlast) begin
          for (int i = 0; i < 4; i++)
            sysmem[{m_aw_q[0].addr[63:5], 5'b0} + 64'(8 * i)] = mst_wdata[64*i +: 64];
          mst_w_cycle.push_back(cycle); mst_w_addr.push_back(m_aw_q[0].addr); mst_w_data.push_back(mst_wdata);
          if (verbose) $display("[%0d] MST W  addr=%h data=%h", cycle, m_aw_q[0].addr, mst_wdata[63:0]);
          m_b_q.push_back(m_aw_q[0].id);
          void'(m_aw_q.pop_front());
        end
      end
      if (mst_bvalid_q && mst_bready) begin
        void'(m_b_q.pop_front());
        mst_b_count++;
      end
      if (mst_arvalid) m_ar_q.push_back(mst_arid);
      if (mst_rvalid_q && mst_rready) void'(m_ar_q.pop_front());
      mst_wready_q <= (m_aw_q.size() != 0);
      mst_bvalid_q <= (m_b_q.size() != 0) && !mst_b_hold;
      mst_bid_q    <= (m_b_q.size() != 0) ? m_b_q[0] : '0;
      mst_rvalid_q <= (m_ar_q.size() != 0);
      mst_rid_q    <= (m_ar_q.size() != 0) ? m_ar_q[0] : '0;
      mst_rlast_q  <= 1'b1;
    end
  end

  function automatic int mst_writes_to(input logic [63:0] lo, input logic [63:0] hi, input int after_cycle);
    int n = 0;
    foreach (mst_w_addr[i]) if (mst_w_addr[i] >= lo && mst_w_addr[i] < hi && mst_w_cycle[i] > after_cycle) n++;
    return n;
  endfunction

  // ------------------------------------------------------------------ device
  typedef struct { logic [9:0] id; logic [23:0] did; logic [63:0] addr; bit tr; } dma_t;
  dma_t dma_q[$];
  int   dma_issued = 0, dma_done = 0;
  int   dma_resp [1024];
  int   dma_done_cycle [1024];
  initial foreach (dma_done_cycle[i]) begin dma_done_cycle[i] = -1; dma_resp[i] = -1; end

  function automatic logic [255:0] dma_data(input logic [9:0] id);
    return {8{22'h2a5a5a, id}};
  endfunction

  // Queue one 32-byte device write. awuser: [31:8] DID, [51:32] PID,
  // [52] PV, [53] translated.
  function automatic void dma_write(input int id, input int did, input logic [63:0] addr, input bit tr = 0);
    dma_t d;
    d.id = 10'(id); d.did = 24'(did); d.addr = addr; d.tr = tr;
    dma_done_cycle[id] = -1; dma_resp[id] = -1;
    dma_q.push_back(d);
    dma_issued++;
  endfunction

  initial begin : dma_driver
    dma_t d;
    bit aw_done, w_done;
    forever begin
      @(negedge clk);
      if (dma_q.size() == 0 || !rstn) continue;
      d = dma_q.pop_front();
      slv_awid = d.id; slv_awaddr = d.addr[39:0];
      slv_awuser = '0; slv_awuser[31:8] = d.did; slv_awuser[53] = d.tr;
      slv_wdata = dma_data(d.id);
      slv_awvalid = 1; slv_wvalid = 1;
      aw_done = 0; w_done = 0;
      while (!aw_done || !w_done) begin
        @(posedge clk);
        if (slv_awvalid && slv_awready) aw_done = 1;
        if (slv_wvalid && slv_wready) w_done = 1;
        @(negedge clk);
        if (aw_done) slv_awvalid = 0;
        if (w_done) slv_wvalid = 0;
      end
      if (verbose) $display("[%0d] DMA accepted id=%0d addr=%h", cycle, d.id, d.addr);
    end
  end

  always @(posedge clk) if (rstn && slv_bvalid) begin
    dma_resp[slv_bid] = slv_bresp;
    dma_done_cycle[slv_bid] = cycle;
    dma_done++;
    if (verbose) $display("[%0d] DMA B id=%0d resp=%0d", cycle, slv_bid, slv_bresp);
  end

  task automatic wait_dma(input int id, input int timeout);
    int t0 = cycle;
    while (dma_done_cycle[id] < 0 && cycle - t0 < timeout) @(posedge clk);
  endtask

  // --------------------------------------------------------- DTI endpoint
  logic [63:0] dti_tx_q[$];
  bit          dti_withhold = 0;       // keep acknowledgements until dti_release
  int          dti_ack_delay = 0;
  typedef struct { logic [63:0] word; int due; } dti_ack_t;
  dti_ack_t    dti_ack_q[$];
  int          dti_inv_reqs = 0, dti_sync_reqs = 0, dti_acks_sent = 0;
  int          dti_sync_cycle [$];      // SYNC requests from the IOMMU
  int          dti_sync_ack_cycle [$];  // SYNC acknowledgements accepted by the IOMMU
  bit          dti_out_start = 1;
  logic [63:0] dti_out_first = 0;

  always @(posedge clk) if (rstn) begin
    if (tr_tvalid) begin
      if (dti_out_start) dti_out_first = tr_tdata;
      if (tr_tlast) begin
        if (dti_out_first[3:0] == 4'hc || dti_out_first[3:0] == 4'hd) begin
          dti_ack_t a;
          a.word = dti_out_first; a.due = cycle + dti_ack_delay;
          dti_ack_q.push_back(a);
          if (dti_out_first[3:0] == 4'hc) dti_inv_reqs++;
          else begin dti_sync_reqs++; dti_sync_cycle.push_back(cycle); end
          if (verbose) $display("[%0d] DTI request type=%h", cycle, dti_out_first[3:0]);
        end
      end
      dti_out_start = tr_tlast;
    end
    if (dti_ack_q.size() != 0 && dti_ack_q[0].due <= cycle && !dti_withhold) begin
      dti_tx_q.push_back(dti_ack_q[0].word);
      void'(dti_ack_q.pop_front());
      dti_acks_sent++;
      if (verbose) $display("[%0d] DTI ack", cycle);
    end
  end

  initial begin : dti_sender
    forever begin
      @(negedge clk);
      while (rstn && dti_tx_q.size() != 0) begin
        tr_rdata = dti_tx_q.pop_front(); tr_rlast = 1; tr_rvalid = 1;
        do @(posedge clk); while (!tr_rready);
        if (tr_rdata[3:0] == 4'hd) dti_sync_ack_cycle.push_back(cycle);
        @(negedge clk);
      end
      tr_rvalid = 0; tr_rlast = 0; tr_rdata = 0;
    end
  end

  // DTI connect/enable request: message type 0, state 1, protocol 1.
  function automatic void dti_connect();
    dti_tx_q.push_back(64'h30);
  endfunction

  // ------------------------------------------------------------------- APB
  bit apb_busy = 0;

  task automatic apb_write(input logic [31:0] addr, input logic [31:0] data);
    while (apb_busy) @(negedge clk);
    apb_busy = 1;
    @(negedge clk);
    paddr = addr; pwdata = data; pwrite = 1; psel = 1; penable = 0;
    @(negedge clk);
    penable = 1;
    do @(negedge clk); while (!pready);
    psel = 0; penable = 0; pwrite = 0;
    @(negedge clk);
    apb_busy = 0;
  endtask

  task automatic apb_read(input logic [31:0] addr, output logic [31:0] data);
    while (apb_busy) @(negedge clk);
    apb_busy = 1;
    @(negedge clk);
    paddr = addr; pwrite = 0; psel = 1; penable = 0;
    @(negedge clk);
    penable = 1;
    do @(negedge clk); while (!pready);
    data = prdata;
    psel = 0; penable = 0;
    @(negedge clk);
    apb_busy = 0;
  endtask

  // Poll addr until (value & mask) == want or timeout cycles elapse.
  task automatic poll_reg(input logic [31:0] addr, input logic [31:0] mask, input logic [31:0] want,
                          input int timeout, output logic [31:0] value, output bit ok);
    int t0 = cycle;
    apb_read(addr, value);
    while ((value & mask) != want && cycle - t0 < timeout) apb_read(addr, value);
    ok = ((value & mask) == want);
  endtask

  // ------------------------------------------------------- software helpers
  int cq_tail = 0;

  function automatic logic [63:0] pt_root(input int did); return PT_BASE + 64'(did) * 64'h3000; endfunction
  function automatic logic [63:0] pt_l1(input int did);   return pt_root(did) + 64'h1000; endfunction
  function automatic logic [63:0] pt_l0(input int did);   return pt_root(did) + 64'h2000; endfunction
  function automatic logic [63:0] pte(input logic [63:0] pa, input logic [63:0] flags);
    return ((pa >> 12) << 10) | flags;
  endfunction
  function automatic logic [63:0] page(input int n); return DATA_BASE + 64'(n) * 64'h1000; endfunction

  // Device context (64-byte format): tc.V, tc.EN_ATS; ta.PSCID; fsc = Sv39 root or Bare.
  function automatic void write_dc(input int did, input bit valid, input bit en_ats, input bit sv39,
                                   input int pscid);
    logic [63:0] base = DDT_BASE + 64'(did) * 64;
    poke(base + 0,  (valid ? 64'h1 : 64'h0) | (en_ats ? 64'h2 : 64'h0));
    poke(base + 8,  64'h0);
    poke(base + 16, 64'(pscid) << 12);
    poke(base + 24, sv39 ? ((64'h8 << 60) | (pt_root(did) >> 12)) : 64'h0);
    for (int i = 4; i < 8; i++) poke(base + 64'(8 * i), 64'h0);
  endfunction

  // Sv39 tables: IOVA 1 GiB + slot*4 KiB -> l0[slot]; root[1] -> l1, l1[0] -> l0.
  function automatic void sv39_tables(input int did);
    poke(pt_root(did) + 8, pte(pt_l1(did), PTE_V));
    poke(pt_l1(did), pte(pt_l0(did), PTE_V));
  endfunction

  function automatic void cq_push(input logic [63:0] dw0, input logic [63:0] dw1);
    logic [63:0] slot = CQ_BASE + 64'((cq_tail % CQ_ENTRIES) * 16);
    poke(slot, dw0);
    poke(slot + 8, dw1);
    cq_tail++;
  endfunction

  function automatic logic [63:0] cmd_iotinval_vma(input bit av, input bit pscv, input int pscid);
    return 64'h1 | (64'(av) << 10) | (64'(pscid) << 12) | (64'(pscv) << 32);
  endfunction
  function automatic logic [63:0] cmd_iofence(input bit pr, input bit pw);
    return 64'h2 | (64'(pr) << 12) | (64'(pw) << 13);
  endfunction
  function automatic logic [63:0] cmd_iodir_inval_ddt(input bit dv, input int did);
    return 64'h3 | (64'(dv) << 33) | (64'(did) << 40);
  endfunction
  function automatic logic [63:0] cmd_ats_inval(input int did);
    return 64'h4 | (64'(did) << 40);
  endfunction

  task automatic cq_publish();
    apb_write(REG_CQT, 32'(cq_tail % CQ_ENTRIES));
  endtask

  task automatic wait_cqh(input int target, input int timeout, output bit ok, output int head);
    logic [31:0] v;
    poll_reg(REG_CQH, 32'hffff_ffff, 32'(target % CQ_ENTRIES), timeout, v, ok);
    head = int'(v);
  endtask

  // Reset, leave the wrapper bypass, optionally connect the DTI endpoint,
  // program the DDT and enable the CQ.
  task automatic iommu_start(input bit connect_dti);
    repeat (5) @(negedge clk);
    rstn = 1;
    repeat (3) @(negedge clk);
    apb_write(REG_CTRL, 32'h0);      // also pulses the internal soft reset
    repeat (3) @(negedge clk);
    if (connect_dti) dti_connect();
    apb_write(REG_DDTP, 32'(((DDT_BASE >> 12) << 10) | 64'h2));   // one-level DDT
    apb_write(REG_CQB, 32'(((CQ_BASE >> 12) << 10) | 64'h5));     // 64 entries
    apb_write(REG_CQB_HI, 32'h0);
    apb_write(REG_CQCSR, 32'h1);
    repeat (16) @(negedge clk);
  endtask

  // ---------------------------------------------------------------- results
  int n_fail = 0;
  task automatic report(input bit pass, input string detail);
    $display("RESULT %s %s %s", scenario, pass ? "PASS" : "FAIL", detail);
    if (!pass) n_fail++;
  endtask

  int max_cycles = 200000;
  always @(posedge clk) if (rstn && cycle > max_cycles) begin
    $display("RESULT %s FAIL simulation did not finish within %0d cycles", scenario, max_cycles);
    $fatal(1, "timeout");
  end

  localparam logic [63:0] FQ_A = 64'h5000;   // fault queue page in data-structure memory
  localparam int CTX_FAULT_DID = 3;          // device context with V=0: every write faults
  localparam logic [31:0] ON = 32'h1_0000, BUSY = 32'h2_0000;

  int delay = 0;

  // Fault queue: one fault record is produced while software clears fqen
  // `delay` cycles after the faulting write was queued. With hold_w, the
  // memory does not accept the record's W until 40 cycles after software has
  // seen fqon=0 (or gave up polling). Software polls fqcsr until fqon=0 and
  // busy=0. Passes when no record write is in flight or starts after that
  // read, and fqon eventually reads 0.
  task automatic fq_race(input bit hold_w, input bit drain_first);
    logic [31:0] v; bit ok, off_ok, off_later;
    int off_at, in_flight = 0, started_after = 0;
    apb_write(REG_FQB, 32'(((FQ_A >> 12) << 10) | 64'h5));
    apb_write(REG_FQB_HI, 32'h0);
    apb_write(REG_FQH, 32'h0);
    apb_write(REG_FQCSR, 32'h1);
    poll_reg(REG_FQCSR, ON | BUSY, ON, 200, v, ok);
    if (hold_w) begin dsw_on = 1; dsw_mode = 0; dsw_lo = FQ_A; dsw_hi = FQ_A + 64'h1000; end
    dma_write(1, CTX_FAULT_DID, page(0));
    if (drain_first) begin
      dsw_on = 0;
      poll_reg(REG_FQT, 32'hffff_ffff, 32'd1, 600, v, ok);
    end
    repeat (delay) @(posedge clk);
    apb_write(REG_FQCSR, 32'h0);
    poll_reg(REG_FQCSR, ON | BUSY, 32'h0, 300, v, off_ok);
    off_at = cycle;
    repeat (40) @(posedge clk);
    dsw_on = 0;
    poll_reg(REG_FQCSR, ON | BUSY, 32'h0, 300, v, off_later);
    repeat (100) @(posedge clk);
    if (off_ok) begin
      // record writes accepted (AW) before the read but answered (B) after it
      foreach (dsw_aw_cycle[i])
        if (dsw_aw_addr[i] >= FQ_A && dsw_aw_addr[i] < FQ_A + 64'h1000) begin
          if (dsw_aw_cycle[i] > off_at) started_after++;
          else if (i >= dsw_b_cycle.size() || dsw_b_cycle[i] > off_at) in_flight++;
        end
    end
    report(in_flight == 0 && started_after == 0 && off_later,
           $sformatf("delay=%0d fqon0_busy0_read_at=%0d record_writes_in_flight_at_read=%0d started_after_read=%0d fqon_eventually_0=%0d",
                     delay, off_ok ? off_at : -1, in_flight, started_after, off_later));
  endtask

  // Command queue: data-structure reads take 60 cycles. Software publishes
  // two IOTINVAL.VMA and clears cqen `delay` cycles after the cqt write,
  // then polls cqcsr until cqen, cqon and busy read 0. Passes when no command
  // fetch is in flight or starts after that read.
  task automatic cq_race(input bit drain_first);
    logic [31:0] v; bit ok, off_ok; int off_at, in_flight = 0, started_after = 0, head;
    ds_lat = 60;
    cq_push(cmd_iotinval_vma(0, 0, 1), SV39_IOVA >> 2);
    cq_push(cmd_iotinval_vma(0, 0, 1), (SV39_IOVA + 64'h1000) >> 2);
    cq_publish();
    if (drain_first) wait_cqh(cq_tail, 2000, ok, head);
    repeat (delay) @(posedge clk);
    apb_write(REG_CQCSR, 32'h0);
    poll_reg(REG_CQCSR, ON | BUSY | 32'h1, 32'h0, 600, v, off_ok);
    off_at = cycle;
    repeat (300) @(posedge clk);
    if (off_ok) begin
      foreach (dsr_ar_cycle[i])
        if (dsr_ar_addr[i] >= CQ_BASE && dsr_ar_addr[i] < CQ_BASE + 64'h1000) begin
          if (dsr_ar_cycle[i] > off_at) started_after++;
          else if (i >= dsr_r_cycle.size() || dsr_r_cycle[i] > off_at) in_flight++;   // R after the read
        end
    end
    report(off_ok && in_flight == 0 && started_after == 0,
           $sformatf("delay=%0d cqon0_busy0_read_at=%0d command_fetches_in_flight_at_read=%0d started_after_read=%0d",
                     delay, off_ok ? off_at : -1, in_flight, started_after));
  endtask

  initial begin : main
    void'($value$plusargs("scenario=%s", scenario));
    void'($value$plusargs("delay=%d", delay));
    verbose = $test$plusargs("verbose");
    for (int did = 0; did < 4; did++) write_dc(did, did != CTX_FAULT_DID, 0, 0, 1);
    iommu_start(0);

    case (scenario)
      "fq_disable_race":                fq_race(1, 0);
      "control_fq_disable_no_hold":     fq_race(0, 0);
      "control_fq_disable_after_drain": fq_race(1, 1);
      "cq_disable_race":                cq_race(0);
      "control_cq_disable_after_drain": cq_race(1);
      default: begin
        $display("RESULT %s FAIL unknown scenario", scenario);
        n_fail++;
      end
    endcase
    if (n_fail != 0) $fatal(1, "scenario failed");
    $finish;
  end
endmodule
