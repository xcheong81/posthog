from typing import cast

from posthog.hogql import ast
from posthog.hogql.query import execute_hogql_query
from posthog.schema import (
    CachedRevenueAnalyticsTopCustomersQueryResponse,
    RevenueAnalyticsTopCustomersQueryResponse,
    RevenueAnalyticsTopCustomersQuery,
)

from .revenue_analytics_query_runner import RevenueAnalyticsQueryRunner
from products.revenue_analytics.backend.views.revenue_analytics_customer_view import RevenueAnalyticsCustomerView
from products.revenue_analytics.backend.views.revenue_analytics_invoice_item_view import RevenueAnalyticsInvoiceItemView


class RevenueAnalyticsTopCustomersQueryRunner(RevenueAnalyticsQueryRunner):
    query: RevenueAnalyticsTopCustomersQuery
    response: RevenueAnalyticsTopCustomersQueryResponse
    cached_response: CachedRevenueAnalyticsTopCustomersQueryResponse

    def to_query(self) -> ast.SelectQuery:
        is_monthly_grouping = self.query.groupBy == "month"

        # This query is missing amount/month columns
        # as we're adding them conditionally below based on the groupBy value
        base_query = ast.SelectQuery(
            select=[
                ast.Alias(alias="name", expr=ast.Constant(value="")),
                ast.Alias(alias="customer_id", expr=ast.Field(chain=["inner", "customer_id"])),
                # If grouping all months together, we'll use the sum of the amount
                # Otherwise, we'll use the amount for the specific month
                ast.Alias(
                    alias="amount",
                    expr=ast.Field(chain=["inner", "amount"])
                    if is_monthly_grouping
                    else ast.Call(name="sum", args=[ast.Field(chain=["inner", "amount"])]),
                ),
                ast.Alias(
                    alias="month",
                    expr=ast.Field(chain=["inner", "month"]) if is_monthly_grouping else ast.Constant(value="all"),
                ),
            ],
            select_from=ast.JoinExpr(table=self.inner_query(), alias="inner"),
            order_by=[ast.OrderExpr(expr=ast.Field(chain=["amount"]), order="DESC")],
            # Only need to group if we're grouping all months together
            group_by=[
                ast.Field(chain=["name"]),
                ast.Field(chain=["customer_id"]),
            ]
            if not is_monthly_grouping
            else [],
            # Limit by month again to limit too many rows if we're spanning more than one month
            # but still grouping them because we're using the sum of the amount
            limit_by=ast.LimitByExpr(n=ast.Constant(value=20), exprs=[ast.Field(chain=["month"])]),
        )

        # If there's a way to join with the customer table, then do it
        if self.revenue_subqueries.customer is not None:
            base_query.select[0] = ast.Alias(
                alias="name", expr=ast.Field(chain=[RevenueAnalyticsCustomerView.get_generic_view_alias(), "name"])
            )
            select_from = cast(ast.JoinExpr, base_query.select_from)
            select_from.next_join = ast.JoinExpr(
                table=self.revenue_subqueries.customer,
                alias=RevenueAnalyticsCustomerView.get_generic_view_alias(),
                join_type="LEFT JOIN",
                constraint=ast.JoinConstraint(
                    constraint_type="ON",
                    expr=ast.CompareOperation(
                        left=ast.Field(chain=[RevenueAnalyticsCustomerView.get_generic_view_alias(), "id"]),
                        right=ast.Field(chain=["inner", "customer_id"]),
                        op=ast.CompareOperationOp.Eq,
                    ),
                ),
            )

        return base_query

    def inner_query(self) -> ast.SelectQuery:
        if self.revenue_subqueries.invoice_item is None:
            # Empty query because there are no invoice items, but still include the right columns
            # to make sure the outer query works
            return ast.SelectQuery(
                select=[
                    ast.Alias(alias="customer_id", expr=ast.Constant(value="")),
                    ast.Alias(alias="month", expr=ast.Constant(value="")),
                    ast.Alias(alias="amount", expr=ast.Constant(value=0)),
                ],
                where=ast.CompareOperation(
                    left=ast.Constant(value=1),
                    right=ast.Constant(value=0),
                    op=ast.CompareOperationOp.Eq,
                ),
            )

        return ast.SelectQuery(
            select=[
                ast.Alias(alias="customer_id", expr=ast.Field(chain=["customer_id"])),
                ast.Alias(
                    alias="month",
                    expr=ast.Call(
                        name="toStartOfMonth",
                        args=[ast.Field(chain=["timestamp"])],
                    ),
                ),
                ast.Alias(
                    alias="amount",
                    expr=ast.Call(
                        name="sum",
                        args=[ast.Field(chain=["amount"])],
                    ),
                ),
            ],
            select_from=self.append_joins(
                ast.JoinExpr(
                    alias=RevenueAnalyticsInvoiceItemView.get_generic_view_alias(),
                    table=self.revenue_subqueries.invoice_item,
                ),
                self.joins_for_properties,
            ),
            where=ast.And(exprs=[self.timestamp_where_clause(), *self.where_property_exprs]),
            group_by=[ast.Field(chain=["customer_id"]), ast.Field(chain=["month"])],
            # Top 20 by month only to avoid too many rows
            limit_by=ast.LimitByExpr(n=ast.Constant(value=20), exprs=[ast.Field(chain=["month"])]),
        )

    def calculate(self):
        response = execute_hogql_query(
            query_type="revenue_analytics_top_customers_query",
            query=self.to_query(),
            team=self.team,
            timings=self.timings,
            modifiers=self.modifiers,
            limit_context=self.limit_context,
        )

        return RevenueAnalyticsTopCustomersQueryResponse(
            results=response.results,
            columns=["name", "customer_id", "amount", "month"],
            modifiers=self.modifiers,
        )
