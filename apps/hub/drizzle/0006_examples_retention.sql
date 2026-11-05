-- The time a request was closed (delivered or declined) and the time its examples were deleted, 90 days after
-- (app/lib/adapter-requests.server.ts: clearExpiredExamples).
ALTER TABLE "adapter_request" DROP CONSTRAINT "adapter_request_data_set";--> statement-breakpoint
ALTER TABLE "adapter_request" ADD COLUMN "closed_at" timestamp with time zone;--> statement-breakpoint
ALTER TABLE "adapter_request" ADD COLUMN "examples_deleted_at" timestamp with time zone;--> statement-breakpoint
-- A request already delivered or declined was closed when its status last changed: its updated_at.
UPDATE "adapter_request" SET "closed_at" = "updated_at" WHERE "status" in ('delivered', 'declined');--> statement-breakpoint
ALTER TABLE "adapter_request" ADD CONSTRAINT "adapter_request_closed" CHECK (("adapter_request"."status" in ('delivered', 'declined')) = ("adapter_request"."closed_at" is not null));--> statement-breakpoint
ALTER TABLE "adapter_request" ADD CONSTRAINT "adapter_request_examples_deleted" CHECK ("adapter_request"."examples_deleted_at" is null or "adapter_request"."examples" = '[]'::jsonb);--> statement-breakpoint
ALTER TABLE "adapter_request" ADD CONSTRAINT "adapter_request_data_set" CHECK (("adapter_request"."example_source" = 'data_set' and "adapter_request"."examples_deleted_at" is null) = ("adapter_request"."data_set_pathname" is not null)
    and ("adapter_request"."data_set_pathname" is null) = ("adapter_request"."data_set_url" is null) and ("adapter_request"."example_source" = 'data_set') = ("adapter_request"."data_set_bytes" is not null)
    and ("adapter_request"."example_source" = 'data_set') = ("adapter_request"."data_set_rows" is not null) and ("adapter_request"."example_source" = 'data_set') = ("adapter_request"."data_set_counts" is not null));