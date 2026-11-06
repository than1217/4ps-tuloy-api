-- DQA essi-mvp-v2 duplicate-claim checks look up the same member and period in other batches.
ALTER TABLE `bcmd_batch_member` ADD KEY `ix_bcmd_member_claim` (`member_ref`, `academic_year`, `semester`);
