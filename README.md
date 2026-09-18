# Ciderlane Return Agent

Ciderlane Return Agent is an evaluated Python customer-support agent for a fictional fast-fashion store. It verifies customers, applies return policies through deterministic tools, and creates mock return labels for eligible items.

## What It Does

- Verifies an order using the order ID and customer email
- Identifies the specific item the customer wants to return
- Checks eligibility against the 30-day, final-sale, and swimwear policies
- Creates a mock return label for eligible standard returns
- Creates persistent support cases for policy exceptions that require human review
- Protects order information until the customer is verified

## Why I Built It

This project explores how to separate an LLM's conversational reasoning from deterministic business rules and side effects. Policy decisions are enforced by Python tools, while unit tests and agent evals check that the system continues to behave correctly.
