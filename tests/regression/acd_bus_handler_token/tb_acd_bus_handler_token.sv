`timescale 1ns/1ps

// Regression test for the ACD slave interface
// (iommu_acd_bus_handler_slv_intf): while every in-flight token is in use,
// no request may be transferred to the translation queue
// (s2q_valid_o && s2q_ready_i), and a transfer must never carry a token that
// is still outstanding.
//
// The token width is the one iommu_wrap uses: 8 (256 tokens) in the lite
// configuration, iommu_acd_pkg::BUS_INFLY_TOKEN_WIDTH (64 tokens) otherwise.
// The translation queue is always ready; B responses are only returned when a
// scenario frees a token. Checks use the module ports only.
//
// Scenarios (+scenario=<name>):
//   pool_full_write        fill every token with writes, then one more write;
//                          free one token after 16 cycles
//   pool_full_read         same, the extra request is a read
//   control_one_token_free fill all tokens but one, then one more write
//
// Run with tests/regression/acd_bus_handler_token/run.py.
module tb_acd_bus_handler_token;
  import iommu_acd_pkg::*;

`ifdef CONFIG_RISCV_IOMMU_BOSC_V2_LITE
  localparam int TOKEN_WIDTH = 8;
`else
  localparam int TOKEN_WIDTH = iommu_acd_pkg::BUS_INFLY_TOKEN_WIDTH;
`endif
  localparam int TOKEN_COUNT = 1 << TOKEN_WIDTH;
  localparam logic [63:0] TARGET_ADDR = 64'h0000_0000_0055_0000;

  logic clk = 0;
  always #5 clk = ~clk;
  logic rstn = 0;

  logic slv_awvalid = 0;  wire slv_awready;  ch_ax_t slv_awpayld = '0;
  logic slv_arvalid = 0;  wire slv_arready;  ch_ax_t slv_arpayld = '0;
  logic b2s_bvalid = 0;   logic [TOKEN_WIDTH-1:0] b2s_token = '0;
  wire s2q_valid;
  logic s2q_ready = 1;
  ch_ax_t s2q_axpayld;
  wire [TOKEN_WIDTH-1:0] s2q_token;
  wire s2q_wr;
  wire [TOKEN_COUNT-1:0] outstanding;

  iommu_acd_bus_handler_slv_intf #(
    .BUS_INFLY_TOKEN_WIDTH(TOKEN_WIDTH),
    .BUS_INFLY_TOKEN_NUM(TOKEN_COUNT)
  ) dut (
    .clk(clk), .rstn(rstn),
    .slv_awvalid_i(slv_awvalid), .slv_awready_o(slv_awready),
    .slv_awpayld_i(slv_awpayld), .slv_aw_device_id_i(24'h1),
    .slv_aw_process_id_i(20'h0), .slv_aw_process_id_valid_i(1'b0),
    .slv_aw_is_translated_i(1'b0),
    .slv_wvalid_i(1'b0), .slv_wready_o(), .slv_wpayld_i('0),
    .slv_bvalid_o(), .slv_bready_i(1'b1), .slv_bpayld_o(),
    .slv_arvalid_i(slv_arvalid), .slv_arready_o(slv_arready),
    .slv_arpayld_i(slv_arpayld), .slv_ar_device_id_i(24'h1),
    .slv_ar_process_id_i(20'h0), .slv_ar_process_id_valid_i(1'b0),
    .slv_ar_is_translated_i(1'b0),
    .slv_rvalid_o(), .slv_rready_i(1'b1), .slv_rpayld_o(),
    .s2w_wvalid_o(), .s2w_wready_i(1'b1), .s2w_wpayld_o(),
    .b2s_bvalid_i(b2s_bvalid), .b2s_bready_o(), .b2s_bpayld_i('0), .b2s_token_i(b2s_token),
    .r2s_rvalid_i(1'b0), .r2s_rready_o(), .r2s_rpayld_i('0), .r2s_token_i('0),
    .s2q_valid_o(s2q_valid), .s2q_ready_i(s2q_ready),
    .s2q_bc_fail_o(), .s2q_wr_o(s2q_wr),
    .s2q_device_id_o(), .s2q_process_id_o(), .s2q_process_id_valid_o(),
    .s2q_is_translated_o(), .s2q_axpayld_o(s2q_axpayld),
    .s2q_token_o(s2q_token), .s2q_w_can_enqueue_hint_i(1'b1),
    .bh2inv_outstanding_list_o(outstanding),
    .bh2inv_outstanding_rw_list_o(), .spare_in(1'b0)
  );

  int cycle = 0;
  int transfers = 0;
  int target_transfers = 0;
  int transfers_while_full = 0;
  int aliased_tokens = 0;
  int first_target_cycle = -1;
  int target_token = -1;

  always @(posedge clk) begin
    if (rstn) begin
      cycle <= cycle + 1;
      if (s2q_valid && s2q_ready) begin
        transfers++;
        if (&outstanding) transfers_while_full++;
        if (outstanding[s2q_token]) aliased_tokens++;
        if (s2q_axpayld.axaddr == TARGET_ADDR) begin
          target_transfers++;
          if (first_target_cycle < 0) begin
            first_target_cycle = cycle;
            target_token = int'(s2q_token);
          end
        end
      end
    end
  end

  task automatic send_aw(input logic [63:0] addr);
    @(negedge clk);
    slv_awpayld.axaddr = addr;
    slv_awvalid = 1;
    do @(posedge clk); while (!slv_awready);
    @(negedge clk);
    slv_awvalid = 0;
  endtask

  task automatic send_ar(input logic [63:0] addr);
    @(negedge clk);
    slv_arpayld.axaddr = addr;
    slv_arvalid = 1;
    do @(posedge clk); while (!slv_arready);
    @(negedge clk);
    slv_arvalid = 0;
  endtask

  task automatic free_token(input int t);
    @(negedge clk);
    b2s_token = TOKEN_WIDTH'(t);
    b2s_bvalid = 1;
    @(negedge clk);
    b2s_bvalid = 0;
  endtask

  string scenario = "";

  initial begin : main
    int fill, n_before, while_full_before_free, target_before_free;
    bit pass, read_target;
    void'($value$plusargs("scenario=%s", scenario));
    slv_awpayld.axsize = 3'd4; slv_awpayld.axburst = 2'b01;
    slv_arpayld.axsize = 3'd4; slv_arpayld.axburst = 2'b01;
    repeat (4) @(posedge clk);
    rstn = 1;

    if (scenario != "pool_full_write" && scenario != "pool_full_read" &&
        scenario != "control_one_token_free") begin
      $display("RESULT %s FAIL unknown scenario", scenario);
      $fatal(1, "unknown scenario");
    end
    fill = (scenario == "control_one_token_free") ? TOKEN_COUNT - 1 : TOKEN_COUNT;
    read_target = (scenario == "pool_full_read");

    for (int i = 0; i < fill; i++) begin
      send_aw(64'h1000 + 64'(i) * 64'h100);
      wait (transfers == i + 1);
    end
    repeat (2) @(posedge clk);
    n_before = transfers;

    if (read_target) send_ar(TARGET_ADDR);
    else send_aw(TARGET_ADDR);
    repeat (16) @(posedge clk);
    while_full_before_free = transfers_while_full;
    target_before_free = target_transfers;

    if (scenario == "control_one_token_free") begin
      pass = target_transfers == 1 && transfers == n_before + 1 && transfers_while_full == 0 &&
             aliased_tokens == 0;
      $display("RESULT %s %s tokens=%0d in_use_before=%0d target_transfers=%0d transfers_while_all_tokens_used=%0d aliased_tokens=%0d",
               scenario, pass ? "PASS" : "FAIL", TOKEN_COUNT, fill, target_transfers,
               transfers_while_full, aliased_tokens);
    end else begin
      // Free token 5; the extra request must now be transferred exactly once.
      free_token(5);
      repeat (16) @(posedge clk);
      pass = target_before_free == 0 && while_full_before_free == 0 && target_transfers == 1 &&
             target_token == 5 && aliased_tokens == 0;
      $display("RESULT %s %s tokens=%0d target_transfers_before_free=%0d transfers_while_all_tokens_used=%0d target_transfers_total=%0d target_token=%0d aliased_tokens=%0d",
               scenario, pass ? "PASS" : "FAIL", TOKEN_COUNT, target_before_free,
               while_full_before_free, target_transfers, target_token, aliased_tokens);
    end
    if (!pass) $fatal(1, "scenario failed");
    $finish;
  end

  initial begin
    repeat (40 * TOKEN_COUNT + 2000) @(posedge clk);
    $display("RESULT %s FAIL timeout", scenario);
    $fatal(1, "timeout");
  end
endmodule
