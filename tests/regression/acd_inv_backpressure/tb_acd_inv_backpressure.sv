`timescale 1ns/1ps

// Regression test for the ACD invalidation engine (iommu_acd_inv): an entry
// must hold inv_tlbcache_req_valid_o until the TLB cache accepts it.
//
// The engine is driven through its message ports, with a TLB-cache model that
// holds inv_tlbcache_req_ready_i low for a while (legal back-pressure) and
// acknowledges each accepted request one cycle later. Each scenario sends
// invalidation messages and then checks, from the ports only, that
//   - every message was accepted (inv_buf_overflow_err_o never set),
//   - every invalidation reached the TLB cache exactly once with its address,
//   - the request payload did not change while it was stalled,
//   - every invalidation was acknowledged on the message port.
//
// Scenarios (+scenario=<name>):
//   control_single_ready      one invalidation, TLB cache ready
//   control_single_stalled    one invalidation, ready held low for 6 cycles
//   two_gap<G>_stall<S>       a second invalidation sent G cycles after the
//                             first request appears, ready released S cycles
//                             after the second message
//   fill_entries_stall<S>     as many invalidations as the engine has entries
//
// Run with tests/regression/acd_inv_backpressure/run.py.
module tb_acd_inv_backpressure;
  import iommu_acd_pkg::*;

  // The lite configuration instantiates the engine with 2 entries
  // (iommu_wrap.sv, INV_IDX_WIDTH 1); the default uses the package value.
`ifdef CONFIG_RISCV_IOMMU_BOSC_V2_LITE
  localparam int INV_IDX_WIDTH = 1;
`else
  localparam int INV_IDX_WIDTH = iommu_acd_pkg::INV_IDX_WIDTH;
`endif
  localparam int ENTRIES = 2 ** INV_IDX_WIDTH;

  logic clk = 0;
  always #5 clk = ~clk;
  logic rstn = 0;

  logic inv_tlbcache_req_ready = 0;
  logic inv_tlbcache_ack_valid = 0;
  INVALID_REQ_TYPE inv_tlbcache_ack = '0;
  wire inv_tlbcache_req_valid;
  INVALID_REQ_TYPE inv_tlbcache_req;
  logic msg_valid = 0;
  wire msg_ready;
  logic [63:0] msg_wdata = 0;
  wire msg_ivalid;
  MSG_INV_ACK_TYPE msg_idata;
  wire msg_ilast;
  wire inv_buf_overflow_err;

  iommu_acd_inv #(.INV_IDX_WIDTH(INV_IDX_WIDTH)) dut (
    .clk(clk), .rstn(rstn),
    .inv_tlbcache_req_valid_o(inv_tlbcache_req_valid),
    .inv_tlbcache_req_ready_i(inv_tlbcache_req_ready),
    .inv_tlbcache_req_o(inv_tlbcache_req),
    .inv_tlbcache_ack_valid_i(inv_tlbcache_ack_valid),
    .inv_tlbcache_ack_i(inv_tlbcache_ack),
    .qinfo2_inv_i('0),
    .inv_ptwackfilter_req_valid_o(),
    .inv_ptwackfilter_req_o(),
    .msg_valid_i(msg_valid), .msg_ready_o(msg_ready), .msg_wdata_i(msg_wdata),
    .msg_ivalid_o(msg_ivalid), .msg_iready_i(1'b1), .msg_idata_o(msg_idata),
    .msg_ilast_o(msg_ilast),
    .inv_buf_overflow_err_o(inv_buf_overflow_err),
    .bh2inv_outstanding_list_i('0),
    .bh2inv_outstanding_rw_list_i('0),
    .spare_in(1'b0)
  );

  int cycle = 0;
  int req_cycles = 0, req_handshakes = 0, acks = 0, overflows = 0, unstable = 0;
  logic [51:0] seen_addr [$];
  bit stalled = 0;
  INVALID_REQ_TYPE stalled_payload;

  always @(posedge clk) begin
    if (rstn) begin
      cycle <= cycle + 1;
      // TLB-cache model: acknowledge an accepted request one cycle later.
      inv_tlbcache_ack_valid <= inv_tlbcache_req_valid && inv_tlbcache_req_ready;
      inv_tlbcache_ack <= inv_tlbcache_req;
      if (inv_tlbcache_req_valid) req_cycles++;
      if (inv_tlbcache_req_valid && inv_tlbcache_req_ready) begin
        req_handshakes++;
        seen_addr.push_back(inv_tlbcache_req.addr);
      end
      if (msg_ivalid && msg_ilast) acks++;
      if (inv_buf_overflow_err) overflows++;
      if (inv_tlbcache_req_valid && !inv_tlbcache_req_ready) begin
        if (!stalled) begin
          stalled = 1;
          stalled_payload = inv_tlbcache_req;
        end else if (inv_tlbcache_req !== stalled_payload) unstable++;
      end else stalled = 0;
    end
  end

  // Three flits: request code and tag, command, invalidation address.
  task automatic send_invalidation(input logic [3:0] tag, input logic [51:0] addr);
    @(negedge clk);
    msg_valid = 1;
    msg_wdata = {56'd0, tag, MSGCODE_INV_REQ};
    @(negedge clk);
    msg_wdata = 64'(INVOPCODE_IOTINVAL);
    @(negedge clk);
    msg_wdata = {2'b00, addr, 10'd0};
    @(negedge clk);
    msg_valid = 0;
    msg_wdata = 0;
  endtask

  string scenario = "";
  logic [51:0] sent [$];

  // Decimal number that follows `key` in s, or -1.
  function automatic int number_after(input string s, input string key);
    int v = -1;
    for (int i = 0; i + key.len() <= s.len(); i++)
      if (s.substr(i, i + key.len() - 1) == key) begin
        v = 0;
        for (int j = i + key.len(); j < s.len() && s[j] >= "0" && s[j] <= "9"; j++) v = v * 10 + (s[j] - "0");
        break;
      end
    return v;
  endfunction

  task automatic check(input int min_req_cycles);
    bit pass;
    int matched = 0;
    foreach (sent[i]) foreach (seen_addr[j]) if (seen_addr[j] == sent[i]) begin matched++; break; end
    pass = overflows == 0 && req_handshakes == sent.size() && matched == sent.size() &&
           acks == sent.size() && unstable == 0 && req_cycles >= min_req_cycles;
    $display("RESULT %s %s entries=%0d sent=%0d tlb_requests=%0d matched_addresses=%0d acks=%0d unstable_payload_cycles=%0d overflow=%0d",
             scenario, pass ? "PASS" : "FAIL", ENTRIES, sent.size(), req_handshakes, matched, acks,
             unstable, overflows);
    if (!pass) $fatal(1, "scenario failed");
  endtask

  initial begin : main
    int gap, stall;
    void'($value$plusargs("scenario=%s", scenario));
    repeat (3) @(negedge clk);
    rstn = 1;
    repeat (2) @(negedge clk);

    if (scenario == "control_single_ready") begin
      inv_tlbcache_req_ready = 1;
      sent.push_back(52'h12345);
      send_invalidation(4'd3, sent[0]);
      repeat (20) @(posedge clk);
      check(1);
    end else if (scenario == "control_single_stalled") begin
      sent.push_back(52'h12345);
      send_invalidation(4'd3, sent[0]);
      repeat (6) @(posedge clk);
      @(negedge clk);
      inv_tlbcache_req_ready = 1;
      repeat (20) @(posedge clk);
      check(2);
    end else if (scenario.substr(0, 6) == "two_gap") begin
      gap = number_after(scenario, "gap");
      stall = number_after(scenario, "stall");
      // The second entry reaches its request state while the arbiter holds
      // the first entry's stalled request.
      sent.push_back(52'h11111);
      sent.push_back(52'h22222);
      send_invalidation(4'd1, sent[0]);
      wait (inv_tlbcache_req_valid === 1'b1);
      repeat (gap) @(posedge clk);
      send_invalidation(4'd2, sent[1]);
      repeat (stall) @(posedge clk);
      @(negedge clk);
      inv_tlbcache_req_ready = 1;
      repeat (40) @(posedge clk);
      check(2);
    end else if (scenario.substr(0, 11) == "fill_entries") begin
      stall = number_after(scenario, "stall");
      for (int i = 0; i < ENTRIES; i++) begin
        sent.push_back(52'h10000 + 52'(i * 'h1111));
        send_invalidation(4'(i), sent[i]);
      end
      repeat (stall) @(posedge clk);
      @(negedge clk);
      inv_tlbcache_req_ready = 1;
      repeat (60) @(posedge clk);
      check(ENTRIES);
    end else begin
      $display("RESULT %s FAIL unknown scenario", scenario);
      $fatal(1, "unknown scenario");
    end
    $finish;
  end

  initial begin
    repeat (2000) @(posedge clk);
    $display("RESULT %s FAIL timeout", scenario);
    $fatal(1, "timeout");
  end
endmodule
